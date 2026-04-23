import { SourceFile } from "ts-morph";

export function detectIntegrations(src: SourceFile) {
  const text = src.getFullText();
  const mongo = new Set<string>();
  const natsPub = new Set<string>();
  const natsSub = new Set<string>();
  const env = new Set<string>();

  // Mongoose: mongoose.model("Name", schema) or model("Name", schema)
  for (const m of text.matchAll(/(?:mongoose\.)?model\(\s*["']([A-Za-z][A-Za-z0-9_]*)["']/g)) {
    mongo.add(m[1]);
  }
  // db.collection("name") / getCollection("name")
  for (const m of text.matchAll(/(?:getCollection|\.collection)\(\s*["']([A-Za-z][A-Za-z0-9_]*)["']/g)) {
    mongo.add(m[1]);
  }
  // @Schema({ collection: "name" })
  for (const m of text.matchAll(/collection\s*:\s*["']([A-Za-z][A-Za-z0-9_]*)["']/g)) {
    mongo.add(m[1]);
  }

  // NATS: jsCtx.publish("subject", ...) / nc.publish("subject", ...)
  for (const m of text.matchAll(/\.publish\(\s*["']([a-zA-Z0-9_.\-\*]+)["']/g)) {
    natsPub.add(m[1]);
  }
  // consume({ subject: "..." }) / subscribe("...")
  for (const m of text.matchAll(/\.subscribe\(\s*["']([a-zA-Z0-9_.\-\*]+)["']/g)) {
    natsSub.add(m[1]);
  }
  for (const m of text.matchAll(/subject\s*:\s*["']([a-zA-Z0-9_.\-\*]+)["']/g)) {
    natsSub.add(m[1]);
  }

  // process.env.FOO / process.env["FOO"]
  for (const m of text.matchAll(/process\.env\.([A-Z][A-Z0-9_]*)/g)) env.add(m[1]);
  for (const m of text.matchAll(/process\.env\[["']([A-Z][A-Z0-9_]*)["']\]/g)) env.add(m[1]);

  return {
    mongo: [...mongo],
    nats_pub: [...natsPub],
    nats_sub: [...natsSub],
    env: [...env],
  };
}
