--- mcp-companion.nvim — Tool parameter-schema normalization
---
--- Shared by every place that hands a tool's `inputSchema` to a downstream JSON
--- consumer (the combiner manifest, the CodeCompanion registration path). Two
--- distinct hazards are handled here so no call site has to remember them:
---
---   1. Empty-object encoding. An empty Lua table `{}` is ambiguous and Neovim's
---      msgpack/JSON encoder emits it as an array `[]`. A strict adapter (e.g.
---      the Copilot HTTP adapter) then rejects the tool with
---      `[] is not of type 'object'` (issue #7). `vim.empty_dict()` pins an empty
---      table to `{}`. This is applied *recursively* so a nested empty object
---      schema is safe too, not just the top-level `properties`.
---
---   2. Structural validity. The top level must be an object schema with a
---      `properties` object, and a `required` that isn't a JSON array is invalid
---      and is dropped.
---
--- @module mcp_companion.schema
local M = {}

--- @param t table
--- @return boolean
local function is_list(t)
    if vim.islist then
        return vim.islist(t)
    end
    return vim.tbl_islist(t)
end

--- Recursively re-encode empty object-position tables as `{}` (not `[]`).
--- Lists stay lists; non-tables pass through untouched.
--- @param value any
--- @return any
local function preserve_objects(value)
    if type(value) ~= "table" then
        return value
    end
    if next(value) == nil then
        return vim.empty_dict()
    end
    if is_list(value) then
        local out = {}
        for i, item in ipairs(value) do
            out[i] = preserve_objects(item)
        end
        return out
    end
    local out = vim.empty_dict()
    for key, item in pairs(value) do
        out[key] = preserve_objects(item)
    end
    return out
end

--- Normalize a tool parameter schema into a strict, safely-encodable object.
--- @param schema any
--- @return table
function M.normalize(schema)
    if type(schema) ~= "table" or next(schema) == nil then
        return { type = "object", properties = vim.empty_dict() }
    end

    local out = vim.deepcopy(schema)
    if out.type == nil then
        out.type = "object"
    end
    if out.type == "object" and out.properties == nil then
        out.properties = {} -- becomes vim.empty_dict() via preserve_objects
    end
    -- `required` must be a JSON array of field names. Coerce a bare string into
    -- a single-element array (the likely intent); drop any other non-list value
    -- (number/bool/object) as uncoercible.
    if out.required ~= nil and not is_list(out.required) then
        if type(out.required) == "string" then
            out.required = { out.required }
        else
            out.required = nil
        end
    end

    return preserve_objects(out)
end

return M
