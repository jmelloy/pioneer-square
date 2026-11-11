#!/usr/bin/env node
import { main } from "../cli.js";
main(process.argv.slice(2), "mcp-server-prod").then((c) => process.exit(c));
