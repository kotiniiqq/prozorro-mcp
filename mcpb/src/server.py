"""Claude Desktop extension entry point: runs the prozorro-mcp server with the arguments from the manifest."""
import sys

from prozorro_mcp.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
