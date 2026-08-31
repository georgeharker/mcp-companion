/**
 * Formatting config for this repo's TypeScript (the plugins/<host>/src trees).
 * pi-lens formats JS/TS via its formatter cascade ["biome", "prettier", ...];
 * without this file biome's defaults apply. This pins prettier(d) instead,
 * configured to match the code as written (no-semi, double quotes, 4-space,
 * 120 cols) so adding the config causes no reformat churn.
 *
 * prettier also claims YAML/JSON/Markdown when the cascade formats them, so
 * the override below keeps those at the standard 2-space style — otherwise
 * every workflow file would get re-indented to this repo's TS-specific
 * 4-space width on the next format pass.
 *
 * @see https://prettier.io/docs/configuration
 * @type {import("prettier").Config}
 */
const config = {
    semi: false,
    singleQuote: false,
    tabWidth: 4,
    useTabs: false,
    trailingComma: "all",
    printWidth: 120,
    overrides: [
        {
            files: ["*.yml", "*.yaml", "*.json", "*.jsonc", "*.md", "*.mdx"],
            options: { tabWidth: 2, printWidth: 120 },
        },
    ],
}

export default config
