import { describe, expect, it } from "vitest";
import { GROUPS, NAV } from "./components/nav";
import { clientConfig } from "./pages/Agents";

const URL = "http://specforge.lab:8000/mcp";
const SSE = URL + "/sse";

describe("MCP guide", () => {
  it("lives in its own group, not at /mcp (that path is the endpoint agents connect to)", () => {
    const page = NAV.find((n) => n.group === "agents");
    expect(page?.to).toBe("/agents");
    expect(NAV.some((n) => n.to === "/mcp" || n.to.startsWith("/mcp/"))).toBe(false);
    expect(GROUPS.find((g) => g.key === "agents")?.title).toBeTruthy();
  });
  it("gives OpenCode a remote server entry", () => {
    const cfg = JSON.parse(clientConfig("opencode", URL, SSE)[0].code);
    expect(cfg.mcp.specforge).toEqual({ type: "remote", url: URL, enabled: true });
  });
  it("gives Roo Code a streamable-http server entry", () => {
    const cfg = JSON.parse(clientConfig("roo", URL, SSE)[0].code);
    expect(cfg.mcpServers.specforge.type).toBe("streamable-http");
    expect(cfg.mcpServers.specforge.url).toBe(URL);
  });
  it("gives Claude Code the CLI command and a project .mcp.json", () => {
    const [cli, file] = clientConfig("claude", URL, SSE);
    expect(cli.code).toBe(`claude mcp add --transport http --scope user specforge ${URL}`);
    expect(JSON.parse(file.code).mcpServers.specforge).toEqual({ type: "http", url: URL });
  });
  it("lists both transports for other clients", () => {
    expect(clientConfig("other", URL, SSE).map((c) => c.code)).toEqual([URL, SSE]);
  });
});
