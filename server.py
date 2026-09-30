from fastmcp import FastMCP

mcp = FastMCP(
    name="email MCP",
)


@mcp.tool
def get_email():
    pass


