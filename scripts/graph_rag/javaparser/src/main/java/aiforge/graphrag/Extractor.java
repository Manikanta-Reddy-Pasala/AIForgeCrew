package aiforge.graphrag;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.ImportDeclaration;
import com.github.javaparser.ast.Modifier;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.body.*;
import com.github.javaparser.ast.expr.*;
import com.github.javaparser.ast.stmt.*;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.ast.type.ReferenceType;
import com.github.javaparser.resolution.UnsolvedSymbolException;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;

import java.io.BufferedWriter;
import java.io.File;
import java.io.IOException;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Walk a Java source tree, parse every .java file with JavaParser and emit
 * one JSON line per file to stdout (or the --out file). Ingester reads the
 * JSONL and pushes nodes/edges to Neo4j.
 *
 * Usage:
 *   java -jar graph-rag-extractor.jar --repo <root> [--out <file.jsonl>]
 *     [--src src/main/java] [--resolve]
 *
 * --resolve enables the JavaSymbolSolver for fully-qualified call + type
 * resolution. Much slower but produces accurate CALLS edges.
 */
public final class Extractor {

    public static void main(String[] args) throws Exception {
        Path repo = null;
        Path out = null;
        List<String> srcRoots = new ArrayList<>();
        boolean resolve = false;
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--repo" -> repo = Paths.get(args[++i]).toAbsolutePath();
                case "--out" -> out = Paths.get(args[++i]).toAbsolutePath();
                case "--src" -> srcRoots.add(args[++i]);
                case "--resolve" -> resolve = true;
                default -> System.err.println("unknown arg: " + args[i]);
            }
        }
        if (repo == null) {
            System.err.println("--repo required");
            System.exit(2);
        }
        if (srcRoots.isEmpty()) srcRoots.add("src/main/java");

        var cfg = new ParserConfiguration();
        cfg.setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_21);
        if (resolve) {
            var ts = new CombinedTypeSolver(new ReflectionTypeSolver());
            for (String s : srcRoots) {
                Path p = repo.resolve(s);
                if (Files.isDirectory(p)) ts.add(new JavaParserTypeSolver(p));
            }
            cfg.setSymbolResolver(new JavaSymbolSolver(ts));
        }
        StaticJavaParser.setConfiguration(cfg);

        ObjectMapper mapper = new ObjectMapper();
        mapper.disable(SerializationFeature.INDENT_OUTPUT);

        BufferedWriter writer = null;
        if (out != null) {
            Files.createDirectories(out.getParent());
            writer = Files.newBufferedWriter(out);
        }
        AtomicInteger count = new AtomicInteger();
        AtomicInteger errors = new AtomicInteger();

        for (String s : srcRoots) {
            Path root = repo.resolve(s);
            if (!Files.isDirectory(root)) continue;
            try (var stream = Files.walk(root)) {
                for (Path p : (Iterable<Path>) stream::iterator) {
                    if (!p.toString().endsWith(".java")) continue;
                    if (p.toString().contains("/target/") || p.toString().contains("/build/")) continue;
                    try {
                        Map<String, Object> record = parseOne(p, repo);
                        String line = mapper.writeValueAsString(record);
                        if (writer != null) { writer.write(line); writer.newLine(); }
                        else { System.out.println(line); }
                        if (count.incrementAndGet() % 200 == 0) {
                            System.err.println("... " + count.get() + " files");
                        }
                    } catch (Exception e) {
                        errors.incrementAndGet();
                        System.err.println("parse fail " + p + ": " + e.getMessage());
                    }
                }
            }
        }
        if (writer != null) writer.close();
        System.err.println("done: " + count.get() + " files, " + errors.get() + " errors");
    }

    private static Map<String, Object> parseOne(Path p, Path repo) throws IOException {
        CompilationUnit cu = StaticJavaParser.parse(p);
        String pkg = cu.getPackageDeclaration().map(x -> x.getNameAsString()).orElse("");
        List<String> imports = new ArrayList<>();
        for (ImportDeclaration imp : cu.getImports()) {
            imports.add(imp.getNameAsString() + (imp.isStatic() ? " (static)" : ""));
        }
        List<Map<String, Object>> classes = new ArrayList<>();

        for (TypeDeclaration<?> td : cu.getTypes()) {
            classes.add(extractType(td, pkg, p, repo));
        }

        return Map.of(
            "file", repo.relativize(p).toString(),
            "package", pkg,
            "imports", imports,
            "classes", classes
        );
    }

    private static Map<String, Object> extractType(TypeDeclaration<?> td, String pkg,
                                                   Path p, Path repo) {
        String simple = td.getNameAsString();
        String fqn = pkg.isEmpty() ? simple : pkg + "." + simple;
        String kind = td.isClassOrInterfaceDeclaration()
            ? (td.asClassOrInterfaceDeclaration().isInterface() ? "interface" : "class")
            : td.isEnumDeclaration() ? "enum"
            : td.isRecordDeclaration() ? "record"
            : "other";

        List<String> annotations = new ArrayList<>();
        for (var a : td.getAnnotations()) annotations.add(a.getNameAsString());

        List<String> extendsList = new ArrayList<>();
        List<String> implementsList = new ArrayList<>();
        if (td.isClassOrInterfaceDeclaration()) {
            var c = td.asClassOrInterfaceDeclaration();
            for (var ex : c.getExtendedTypes()) extendsList.add(ex.getNameAsString());
            for (var im : c.getImplementedTypes()) implementsList.add(im.getNameAsString());
        }

        // Class-level @RequestMapping base path
        String classPathPrefix = "";
        for (var a : td.getAnnotations()) {
            if (a.getNameAsString().equals("RequestMapping")) {
                classPathPrefix = firstStringMember(a).orElse("");
            }
        }

        // Fields (autowired / final — dependency edges)
        List<Map<String, Object>> fields = new ArrayList<>();
        for (FieldDeclaration fd : td.getFields()) {
            boolean isFinal = fd.isFinal();
            boolean hasInject = fd.getAnnotations().stream()
                .map(x -> x.getNameAsString())
                .anyMatch(n -> n.equals("Autowired") || n.equals("Inject")
                         || n.equals("Value") || n.equals("Resource"));
            if (!(isFinal || hasInject)) continue;
            String type = fd.getVariables().isEmpty() ? ""
                : fd.getVariable(0).getTypeAsString();
            for (var v : fd.getVariables()) {
                fields.add(Map.of(
                    "name", v.getNameAsString(),
                    "type", stripGenerics(type),
                    "final", isFinal,
                    "inject", hasInject
                ));
            }
        }

        List<Map<String, Object>> methods = new ArrayList<>();
        for (MethodDeclaration md : td.getMethods()) {
            methods.add(extractMethod(md, classPathPrefix, fqn));
        }
        for (ConstructorDeclaration cd : td.getConstructors()) {
            methods.add(extractConstructor(cd, fqn));
        }

        int startLine = td.getBegin().map(pos -> pos.line).orElse(0);
        int endLine = td.getEnd().map(pos -> pos.line).orElse(0);
        String javadoc = td.getJavadocComment().map(jc -> jc.getContent().strip()).orElse("");

        // Spring flags
        boolean transactional = annotations.contains("Transactional");
        boolean async_ = annotations.contains("Async");
        boolean scheduled = annotations.contains("Scheduled");
        boolean cacheable = annotations.contains("Cacheable");

        String layer = classifyLayer(annotations, fqn);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("fqn", fqn);
        out.put("simple", simple);
        out.put("kind", kind);
        out.put("package", pkg);
        out.put("layer", layer);
        out.put("start_line", startLine);
        out.put("loc", Math.max(0, endLine - startLine + 1));
        out.put("annotations", annotations);
        out.put("extends", extendsList);
        out.put("implements", implementsList);
        out.put("fields", fields);
        out.put("methods", methods);
        out.put("javadoc", javadoc);
        out.put("transactional", transactional);
        out.put("async", async_);
        out.put("scheduled", scheduled);
        out.put("cacheable", cacheable);
        return out;
    }

    private static Map<String, Object> extractMethod(MethodDeclaration md,
                                                      String classPathPrefix,
                                                      String classFqn) {
        String name = md.getNameAsString();
        List<String> paramTypes = new ArrayList<>();
        List<String> paramNames = new ArrayList<>();
        for (Parameter p : md.getParameters()) {
            paramTypes.add(stripGenerics(p.getTypeAsString()));
            paramNames.add(p.getNameAsString());
        }
        String returnType = md.getTypeAsString();
        int line = md.getBegin().map(pos -> pos.line).orElse(0);
        int endLine = md.getEnd().map(pos -> pos.line).orElse(line);

        List<String> annotations = new ArrayList<>();
        for (var a : md.getAnnotations()) annotations.add(a.getNameAsString());

        // Endpoint extraction
        Map<String, Object> endpoint = null;
        for (var a : md.getAnnotations()) {
            String an = a.getNameAsString();
            String http = switch (an) {
                case "GetMapping" -> "GET";
                case "PostMapping" -> "POST";
                case "PutMapping" -> "PUT";
                case "DeleteMapping" -> "DELETE";
                case "PatchMapping" -> "PATCH";
                case "RequestMapping" -> "ANY";
                default -> null;
            };
            if (http != null) {
                String sub = firstStringMember(a).orElse("");
                endpoint = Map.of(
                    "http", http,
                    "path", (classPathPrefix + sub).replace("//", "/"),
                    "handler", classFqn + "#" + name + ":" + line
                );
                break;
            }
        }

        // Method invocations — collect (scope, method, resolvedFqn?)
        List<Map<String, Object>> calls = new ArrayList<>();
        md.findAll(MethodCallExpr.class).forEach(mce -> {
            String scope = mce.getScope().map(Object::toString).orElse("");
            String mname = mce.getNameAsString();
            String resolved = null;
            try {
                var r = mce.resolve();
                resolved = r.getQualifiedSignature();
            } catch (Throwable t) {
                // symbol-solver miss; skip resolution
            }
            calls.add(Map.of(
                "scope", truncate(scope, 120),
                "method", mname,
                "resolved", resolved == null ? "" : resolved
            ));
        });

        // Exceptions thrown
        List<String> throwsList = new ArrayList<>();
        for (var t : md.getThrownExceptions()) throwsList.add(t.asString());

        // Local variable types (var → type)
        Map<String, String> locals = new LinkedHashMap<>();
        md.findAll(VariableDeclarationExpr.class).forEach(vd -> {
            for (var v : vd.getVariables()) {
                locals.put(v.getNameAsString(), stripGenerics(v.getTypeAsString()));
            }
        });

        // Full body source (capped)
        String body = md.getBody().map(b -> b.toString()).orElse("");
        if (body.length() > 4000) body = body.substring(0, 4000);

        String javadoc = md.getJavadocComment().map(jc -> jc.getContent().strip()).orElse("");

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("fqn", classFqn + "#" + name + ":" + line);
        out.put("name", name);
        out.put("sig", "(" + String.join(", ", paramTypes) + ")");
        out.put("return_type", returnType);
        out.put("line", line);
        out.put("loc", Math.max(0, endLine - line + 1));
        out.put("annotations", annotations);
        out.put("param_types", paramTypes);
        out.put("param_names", paramNames);
        out.put("throws", throwsList);
        out.put("endpoint", endpoint);
        out.put("calls", calls);
        out.put("locals", locals);
        out.put("body", body);
        out.put("javadoc", javadoc);
        out.put("transactional", annotations.contains("Transactional"));
        out.put("async", annotations.contains("Async"));
        out.put("scheduled", annotations.contains("Scheduled"));
        out.put("cacheable", annotations.contains("Cacheable"));
        return out;
    }

    private static Map<String, Object> extractConstructor(ConstructorDeclaration cd, String classFqn) {
        String name = cd.getNameAsString();
        int line = cd.getBegin().map(pos -> pos.line).orElse(0);
        int endLine = cd.getEnd().map(pos -> pos.line).orElse(line);
        List<String> paramTypes = new ArrayList<>();
        List<String> paramNames = new ArrayList<>();
        for (Parameter p : cd.getParameters()) {
            paramTypes.add(stripGenerics(p.getTypeAsString()));
            paramNames.add(p.getNameAsString());
        }
        List<String> annotations = new ArrayList<>();
        for (var a : cd.getAnnotations()) annotations.add(a.getNameAsString());
        String body = cd.getBody().toString();
        if (body.length() > 4000) body = body.substring(0, 4000);
        String javadoc = cd.getJavadocComment().map(jc -> jc.getContent().strip()).orElse("");

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("fqn", classFqn + "#<init>:" + line);
        out.put("name", "<init>");
        out.put("sig", "(" + String.join(", ", paramTypes) + ")");
        out.put("return_type", "");
        out.put("line", line);
        out.put("loc", Math.max(0, endLine - line + 1));
        out.put("annotations", annotations);
        out.put("param_types", paramTypes);
        out.put("param_names", paramNames);
        out.put("throws", List.of());
        out.put("endpoint", null);
        out.put("calls", List.of());
        out.put("locals", Map.of());
        out.put("body", body);
        out.put("javadoc", javadoc);
        out.put("transactional", false);
        out.put("async", false);
        out.put("scheduled", false);
        out.put("cacheable", false);
        return out;
    }

    private static Optional<String> firstStringMember(AnnotationExpr a) {
        if (a.isSingleMemberAnnotationExpr()) {
            Expression e = a.asSingleMemberAnnotationExpr().getMemberValue();
            if (e.isStringLiteralExpr()) return Optional.of(e.asStringLiteralExpr().getValue());
            if (e.isArrayInitializerExpr()) {
                for (var v : e.asArrayInitializerExpr().getValues()) {
                    if (v.isStringLiteralExpr()) return Optional.of(v.asStringLiteralExpr().getValue());
                }
            }
        } else if (a.isNormalAnnotationExpr()) {
            for (var pair : a.asNormalAnnotationExpr().getPairs()) {
                String n = pair.getNameAsString();
                if ((n.equals("value") || n.equals("path")) && pair.getValue().isStringLiteralExpr()) {
                    return Optional.of(pair.getValue().asStringLiteralExpr().getValue());
                }
            }
        }
        return Optional.empty();
    }

    private static String classifyLayer(List<String> annotations, String fqn) {
        var an = new HashSet<>(annotations);
        if (an.contains("RestController") || an.contains("Controller")) return "controller";
        if (an.contains("Service")) return "service";
        if (an.contains("Repository")) return "repository";
        if (an.contains("Configuration")) return "config";
        if (an.contains("Component")) return "component";
        String l = fqn.toLowerCase(Locale.ROOT);
        if (l.endsWith("controller")) return "controller";
        if (l.endsWith("service") || l.endsWith("serviceimpl")) return "service";
        if (l.endsWith("repository") || l.endsWith("dao")) return "repository";
        if (l.contains("saga") || l.contains("workflow")) return "workflow";
        if (l.endsWith("mapper")) return "mapper";
        if (l.endsWith("dto") || l.contains("/model/") || l.contains(".model.")) return "model";
        if (l.endsWith("config") || l.endsWith("configuration")) return "config";
        return "other";
    }

    private static String stripGenerics(String t) {
        if (t == null) return "";
        int i = t.indexOf('<');
        return i >= 0 ? t.substring(0, i) : t;
    }

    private static String truncate(String s, int n) {
        if (s == null) return "";
        return s.length() <= n ? s : s.substring(0, n);
    }
}
