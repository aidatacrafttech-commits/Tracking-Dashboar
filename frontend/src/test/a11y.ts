import axe from "axe-core";

/**
 * Runs axe-core against the rendered DOM. Colour contrast is checked in the browser
 * (Playwright) because jsdom has no layout or computed colours.
 */
export async function axeViolations(root: Element = document.body): Promise<string[]> {
  const result = await axe.run(root, {
    rules: { "color-contrast": { enabled: false }, region: { enabled: false } },
  });
  return result.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`);
}
