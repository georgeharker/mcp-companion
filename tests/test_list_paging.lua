-- Client:request_all follows nextCursor across pages (the combiner pages large lists).
-- Run: nvim --headless --noplugin -u NONE -c "set rtp+=$PWD" -c "luafile tests/test_list_paging.lua" -c "qa!"

local pass, fail = 0, 0
local function ok(cond, name)
    if cond then
        pass = pass + 1
        print("  PASS: " .. name)
    else
        fail = fail + 1
        print("  FAIL: " .. name)
    end
end

local Client = require("mcp_companion.combiner.client")

--- A client whose request() serves canned pages, recording the cursors it was sent.
local function paged_client(pages)
    local client = setmetatable({ sent = {} }, { __index = Client })
    function client:request(method, params, callback)
        table.insert(self.sent, { method = method, cursor = params and params.cursor })
        local index = params and params.cursor and tonumber(params.cursor:match("^p(%d+)$")) or 1
        local page = pages[index]
        if page == nil then
            callback("no such page", nil)
            return
        end
        callback(nil, page)
    end
    return client
end

-- Three pages, then no cursor: every item, in order, from three requests.
local client = paged_client({
    { resources = { { uri = "a" }, { uri = "b" } }, nextCursor = "p2", resourceTemplates = { "t" } },
    { resources = { { uri = "c" } }, nextCursor = "p3" },
    { resources = { { uri = "d" } } },
})
local got, first
client:request_all("resources/list", "resources", function(err, items, first_page)
    ok(err == nil, "no error across pages")
    got, first = items, first_page
end)
ok(got and #got == 4, "all items from every page")
ok(got and got[1].uri == "a" and got[4].uri == "d", "items keep page order")
ok(#client.sent == 3, "one request per page")
ok(client.sent[1].cursor == nil and client.sent[2].cursor == "p2", "cursor passed back")
ok(first and first.resourceTemplates[1] == "t", "first page returned for side fields")

-- A single unpaged response is one request.
local single = paged_client({ { tools = { { name = "x" } } } })
local tools
single:request_all("tools/list", "tools", function(_, items)
    tools = items
end)
ok(tools and #tools == 1 and #single.sent == 1, "unpaged list is one request")

-- An error mid-walk is reported, not a partial list.
local broken = paged_client({ { prompts = { { name = "p" } }, nextCursor = "p9" } })
local perr, pitems
broken:request_all("prompts/list", "prompts", function(err, items)
    perr, pitems = err, items
end)
ok(perr == "no such page" and pitems == nil, "a failed page fails the walk")

-- A cursor that never ends stops at the page bound.
local looping = setmetatable({ sent = 0 }, { __index = Client })
function looping:request(_, _, callback)
    self.sent = self.sent + 1
    callback(nil, { tools = { { name = "t" .. self.sent } }, nextCursor = "again" })
end
local lerr, litems
looping:request_all("tools/list", "tools", function(err, items)
    lerr, litems = err, items
end)
ok(lerr == nil and litems and #litems == 250, "runaway cursor stops at 250 pages")

-- refresh_capabilities fetches resource templates from resources/templates/list (paged)
-- and groups them under their server, like resources.
local updates = {}
package.loaded["mcp_companion.state"] = {
    update = function(key, value)
        updates[key] = value
    end,
    emit = function() end,
    field = function()
        return nil
    end,
}
local responses = {
    ["tools/list"] = { { tools = { { name = "mockup_echo" } } } },
    ["resources/list"] = { { resources = { { uri = "data://mockup/mock/readme" } } } },
    ["prompts/list"] = { { prompts = {} } },
    ["resources/templates/list"] = {
        { resourceTemplates = { { uriTemplate = "svg://mockup/{id}/svg" } }, nextCursor = "p2" },
        { resourceTemplates = { { uriTemplate = "notes://mockup/{name}" } } },
    },
}
local caps = setmetatable({ _known_server_names = { "mockup" }, asked = {} }, { __index = Client })
function caps:request(method, params, callback)
    table.insert(self.asked, method)
    local index = params and params.cursor and tonumber(params.cursor:match("^p(%d+)$")) or 1
    callback(nil, responses[method][index])
end
local refreshed = false
caps:refresh_capabilities(function()
    refreshed = true
end)
ok(refreshed, "refresh completes with the templates request")
ok(#caps.resource_templates == 2, "templates come from resources/templates/list, every page")
local mockup
for _, srv in ipairs(updates.servers or {}) do
    if srv.name == "mockup" then
        mockup = srv
    end
end
ok(mockup and #mockup.resource_templates == 2, "templates grouped under their server")
ok(mockup and #mockup.resources == 1, "resources still grouped")

print(string.format("=== Results: %d passed, %d failed ===", pass, fail))
if fail > 0 then
    vim.cmd("cq")
end
