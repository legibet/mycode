import type { PhrasingContent, Root, Text } from "mdast";
import type { InlineMath } from "mdast-util-math";
import { SKIP, visit } from "unist-util-visit";

// Keep math pairs inside one Markdown text node. This prevents currency from
// pairing across emphasis, and the final guard rejects ranges such as $5 to $10.
const SINGLE_DOLLAR_MATH = /(?<!\\)\$(?=\S)[^$\n]*?[^\s$]\$(?!\d)/g;

export function remarkSingleDollarMath() {
  return (tree: Root) => {
    visit(tree, "text", (node: Text, index, parent) => {
      if (index === undefined || !parent || !node.value.includes("$")) return;

      const replacements: PhrasingContent[] = [];
      let cursor = 0;

      for (const match of node.value.matchAll(SINGLE_DOLLAR_MATH)) {
        if (match.index > cursor) {
          replacements.push({
            type: "text",
            value: node.value.slice(cursor, match.index),
          });
        }

        const value = match[0].slice(1, -1);
        const math: InlineMath = {
          type: "inlineMath",
          value,
          data: {
            hName: "code",
            hProperties: { className: ["language-math", "math-inline"] },
            hChildren: [{ type: "text", value }],
          },
        };
        replacements.push(math);
        cursor = match.index + match[0].length;
      }

      if (cursor === 0) return;

      if (cursor < node.value.length) {
        replacements.push({ type: "text", value: node.value.slice(cursor) });
      }

      const children = parent.children as PhrasingContent[];
      children.splice(index, 1, ...replacements);
      return [SKIP, index + replacements.length];
    });
  };
}
