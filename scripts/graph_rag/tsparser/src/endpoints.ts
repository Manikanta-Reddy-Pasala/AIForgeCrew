import { SourceFile, Node, SyntaxKind, CallExpression } from "ts-morph";

const ROUTER_METHODS = new Set(["get", "post", "put", "patch", "delete", "options", "head", "all"]);
const NEST_DECORATORS = new Set(["Get", "Post", "Put", "Patch", "Delete", "Options", "Head"]);
const FETCH_NAMES = new Set(["fetch", "axios", "request"]);

function literalString(arg: Node | undefined): string | null {
  if (!arg) return null;
  const kind = arg.getKindName();
  if (kind === "StringLiteral" || kind === "NoSubstitutionTemplateLiteral") {
    return arg.getText().slice(1, -1);
  }
  if (kind === "TemplateExpression") {
    // template literal with ${} -> keep the static prefix
    const txt = arg.getText().slice(1, -1);
    return txt.split("${")[0] || null;
  }
  return null;
}

export function detectEndpoints(src: SourceFile, _repo: string) {
  const endpoints: any[] = [];
  const externalEndpoints: any[] = [];

  // 1. express/fastify:  router.get("/path", handler)
  src.forEachDescendant(node => {
    if (node.getKindName() !== "CallExpression") return;
    const call = node as CallExpression;
    const expr = call.getExpression();
    if (expr.getKindName() !== "PropertyAccessExpression") return;
    const method = (expr as any).getName?.() as string | undefined;
    if (!method || !ROUTER_METHODS.has(method)) return;
    const args = call.getArguments();
    const pathStr = literalString(args[0]);
    if (!pathStr) return;
    endpoints.push({
      http: method.toUpperCase(),
      path: pathStr,
      framework: "express",
      line: call.getStartLineNumber(),
    });
  });

  // 2. NestJS decorators @Get("/path") on methods
  for (const c of src.getClasses()) {
    const ctrlAnn = c.getDecorators().find(d => d.getName() === "Controller");
    const base = ctrlAnn ? (literalString(ctrlAnn.getArguments()[0]) ?? "") : "";
    for (const m of c.getMethods()) {
      for (const d of m.getDecorators()) {
        if (!NEST_DECORATORS.has(d.getName())) continue;
        const sub = literalString(d.getArguments()[0]) ?? "";
        const full = "/" + [base, sub].filter(Boolean).join("/").replace(/\/+/g, "/");
        endpoints.push({
          http: d.getName().toUpperCase(),
          path: full.replace(/^\/+/, "/"),
          framework: "nest",
          controller: c.getName(),
          method: m.getName(),
          line: m.getStartLineNumber(),
        });
      }
    }
  }

  // 3. External calls:  fetch("..."), axios.get("...")
  src.forEachDescendant(node => {
    if (node.getKindName() !== "CallExpression") return;
    const call = node as CallExpression;
    const expr = call.getExpression();
    let name = "";
    if (expr.getKindName() === "Identifier") {
      name = expr.getText();
    } else if (expr.getKindName() === "PropertyAccessExpression") {
      name = (expr as any).getName?.() ?? "";
    }
    if (!FETCH_NAMES.has(name) && !ROUTER_METHODS.has(name.toLowerCase())) return;
    const url = literalString(call.getArguments()[0]);
    if (!url || !/^https?:|^\//.test(url)) return;
    externalEndpoints.push({
      url,
      via: name,
      line: call.getStartLineNumber(),
    });
  });

  return { endpoints, externalEndpoints };
}
