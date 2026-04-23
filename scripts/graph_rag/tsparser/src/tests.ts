import { SourceFile, Node } from "ts-morph";

const TEST_CALLS = new Set(["test", "it", "describe"]);

export function detectTests(src: SourceFile, _repo: string) {
  const file = src.getFilePath();
  if (!/\.(test|spec)\.(ts|tsx|js|jsx)$/.test(file)) return [];
  const out: any[] = [];
  src.forEachDescendant(node => {
    if (node.getKindName() !== "CallExpression") return;
    const expr = (node as any).getExpression?.();
    if (!expr) return;
    const name = expr.getText();
    if (!TEST_CALLS.has(name)) return;
    const args = (node as any).getArguments?.() || [];
    const a0 = args[0];
    if (!a0) return;
    const k = a0.getKindName();
    if (k !== "StringLiteral" && k !== "NoSubstitutionTemplateLiteral") return;
    out.push({
      kind: name === "describe" ? "suite" : "test",
      name: a0.getText().slice(1, -1),
      line: (node as any).getStartLineNumber?.(),
    });
  });
  return out;
}
