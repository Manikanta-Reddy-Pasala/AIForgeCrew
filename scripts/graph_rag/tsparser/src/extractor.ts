#!/usr/bin/env node
/**
 * Domain-layer TS/React/Node extractor. Emits JSONL compatible with
 * ingest_jsonl.py. SCIP handles the symbol/call graph uniformly; this
 * extractor focuses on framework-specific signals SCIP does not expose:
 *
 *   - React components (function + class) w/ hooks used
 *   - REST endpoints (express/fastify/nest)
 *   - fetch/axios URL literals -> ExternalEndpoint
 *   - mongoose/mongodb collection names
 *   - nats-js publish/subscribe
 *   - process.env.X -> EnvVar
 *   - feature-flag style constants
 */
import { Project, SourceFile, Node, SyntaxKind, CallExpression } from "ts-morph";
import * as fs from "fs";
import * as path from "path";
import yargs from "yargs";
import { hideBin } from "yargs/helpers";

import { detectComponents } from "./components";
import { detectEndpoints } from "./endpoints";
import { detectIntegrations } from "./integrations";
import { detectTests } from "./tests";

interface Rec {
  lang: "ts";
  repo: string;
  file: string;
  kind: "file";
  package: string;
  imports: string[];
  classes: any[];
  components: any[];
  functions: any[];
  endpoints: any[];
  externalEndpoints: any[];
  integrations: { mongo: string[]; nats_pub: string[]; nats_sub: string[]; env: string[] };
  tests: any[];
}

function findTsConfig(repo: string): string | undefined {
  const tryPaths = [
    "tsconfig.json",
    "tsconfig.app.json",
    "packages/tsconfig.json",
  ];
  for (const p of tryPaths) {
    const full = path.join(repo, p);
    if (fs.existsSync(full)) return full;
  }
  return undefined;
}

function filePackage(file: string, repo: string): string {
  const rel = path.relative(repo, file);
  return path.dirname(rel).replace(/[\\/]/g, ".");
}

function extractFile(src: SourceFile, repo: string, repoName: string): Rec {
  const file = src.getFilePath();
  const imports = src.getImportDeclarations().map(i => i.getModuleSpecifierValue());

  const classes = src.getClasses().map(c => ({
    kind: "class",
    simple: c.getName() ?? "Anonymous",
    fqn: `${filePackage(file, repo)}.${c.getName() ?? "Anonymous"}`,
    annotations: c.getDecorators().map(d => d.getName()),
    methods: c.getMethods().map(m => ({
      kind: "method",
      name: m.getName(),
      sig: m.getSignature()?.getDeclaration()?.getText()?.slice(0, 200) ?? "",
      return_type: m.getReturnType().getText(),
      annotations: m.getDecorators().map(d => d.getName()),
      start_line: m.getStartLineNumber(),
      end_line: m.getEndLineNumber(),
      body_snippet: m.getBodyText()?.slice(0, 4096) ?? "",
      is_async: m.isAsync(),
    })),
  }));

  const functions = src.getFunctions().map(fn => ({
    kind: "function",
    name: fn.getName() ?? "anonymous",
    fqn: `${filePackage(file, repo)}.${fn.getName() ?? "anonymous"}`,
    sig: fn.getSignature()?.getDeclaration()?.getText()?.slice(0, 200) ?? "",
    return_type: fn.getReturnType().getText(),
    annotations: [],
    start_line: fn.getStartLineNumber(),
    end_line: fn.getEndLineNumber(),
    body_snippet: fn.getBodyText()?.slice(0, 4096) ?? "",
    is_async: fn.isAsync(),
  }));

  const components = detectComponents(src, repo);
  const { endpoints, externalEndpoints } = detectEndpoints(src, repo);
  const integrations = detectIntegrations(src);
  const tests = detectTests(src, repo);

  return {
    lang: "ts",
    repo: repoName,
    file: path.relative(repo, file),
    kind: "file",
    package: filePackage(file, repo),
    imports,
    classes,
    components,
    functions,
    endpoints,
    externalEndpoints,
    integrations,
    tests,
  };
}

async function main() {
  const argv = await yargs(hideBin(process.argv))
    .option("repo", { type: "string", demandOption: true })
    .option("out", { type: "string", demandOption: true })
    .option("files", { type: "array", string: true, default: [] as string[] })
    .option("include", { type: "string", default: "src/**/*.{ts,tsx,js,jsx}" })
    .parse();

  const repoName = path.basename(argv.repo);
  const tsconfig = findTsConfig(argv.repo);

  const project = tsconfig
    ? new Project({ tsConfigFilePath: tsconfig, skipAddingFilesFromTsConfig: true })
    : new Project({ compilerOptions: { allowJs: true, jsx: 4 /* ReactJSX */ } });

  const patterns = argv.files.length
    ? argv.files.map(f => path.resolve(argv.repo, f))
    : [path.join(argv.repo, argv.include)];

  project.addSourceFilesAtPaths(patterns);

  const out = fs.createWriteStream(argv.out, { encoding: "utf-8" });
  let count = 0;
  for (const src of project.getSourceFiles()) {
    try {
      const rec = extractFile(src, argv.repo, repoName);
      out.write(JSON.stringify(rec) + "\n");
      count++;
    } catch (e) {
      console.error(`skip ${src.getFilePath()}: ${(e as Error).message}`);
    }
  }
  out.end();
  console.log(`extracted ${count} files from ${repoName} -> ${argv.out}`);
}

main().catch(e => {
  console.error(e);
  process.exit(1);
});
