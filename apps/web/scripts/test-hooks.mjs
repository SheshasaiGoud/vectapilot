/**
 * Lets `node --test` import the widget's `.tsx` components (M8 PR 4).
 *
 * Node 24 runs `.ts` files itself (type stripping: types are erased, nothing is compiled), but it
 * does not understand JSX. This hook compiles each `.tsx` file with TypeScript's own
 * `transpileModule` (already a dev dependency: no new package, D8) into an ES module that uses
 * React's automatic JSX runtime, exactly as Next.js compiles it. Everything else is loaded by Node
 * unchanged. Used as `node --import ./scripts/test-hooks.mjs --test ...` (`npm run test:unit`).
 */

import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";

import ts from "typescript";

registerHooks({
  load(url, context, nextLoad) {
    if (!url.startsWith("file:") || !url.endsWith(".tsx")) return nextLoad(url, context);
    const fileName = fileURLToPath(url);
    const { outputText } = ts.transpileModule(readFileSync(fileName, "utf8"), {
      fileName,
      compilerOptions: {
        jsx: ts.JsxEmit.ReactJSX,
        module: ts.ModuleKind.ESNext,
        target: ts.ScriptTarget.ES2022,
        inlineSourceMap: true,
      },
    });
    return { format: "module", source: outputText, shortCircuit: true };
  },
});
