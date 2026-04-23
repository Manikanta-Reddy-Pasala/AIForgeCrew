import { SourceFile, Node, FunctionDeclaration, VariableDeclaration, ClassDeclaration } from "ts-morph";

const HOOK_PREFIX = /^use[A-Z]/;

function collectHooks(node: Node): string[] {
  const hooks = new Set<string>();
  node.forEachDescendant(d => {
    if (d.getKindName() === "CallExpression") {
      const name = (d as any).getExpression?.()?.getText?.();
      if (name && HOOK_PREFIX.test(name) && name.length < 40) hooks.add(name);
    }
  });
  return [...hooks];
}

function returnsJsx(node: Node): boolean {
  let found = false;
  node.forEachDescendant(d => {
    const kind = d.getKindName();
    if (kind === "JsxElement" || kind === "JsxSelfClosingElement" ||
        kind === "JsxFragment") {
      found = true;
    }
  });
  return found;
}

export function detectComponents(src: SourceFile, _repo: string) {
  const comps: any[] = [];

  // Function declarations
  for (const fn of src.getFunctions()) {
    const name = fn.getName();
    if (!name || !/^[A-Z]/.test(name)) continue;
    if (!returnsJsx(fn)) continue;
    comps.push({
      kind: "component",
      name,
      style: "function",
      start_line: fn.getStartLineNumber(),
      end_line: fn.getEndLineNumber(),
      hooks: collectHooks(fn),
    });
  }

  // const Foo = () => ...  variable arrow components
  for (const v of src.getVariableDeclarations()) {
    const name = v.getName();
    if (!/^[A-Z]/.test(name)) continue;
    const init = v.getInitializer();
    if (!init) continue;
    const k = init.getKindName();
    if (k !== "ArrowFunction" && k !== "FunctionExpression") continue;
    if (!returnsJsx(init)) continue;
    comps.push({
      kind: "component",
      name,
      style: "arrow",
      start_line: v.getStartLineNumber(),
      end_line: v.getEndLineNumber(),
      hooks: collectHooks(init),
    });
  }

  // Class components extending React.Component
  for (const c of src.getClasses()) {
    const ext = c.getExtends();
    if (!ext) continue;
    const t = ext.getText();
    if (!/Component|PureComponent/.test(t)) continue;
    comps.push({
      kind: "component",
      name: c.getName() ?? "Anonymous",
      style: "class",
      start_line: c.getStartLineNumber(),
      end_line: c.getEndLineNumber(),
      hooks: [],
    });
  }

  return comps;
}
