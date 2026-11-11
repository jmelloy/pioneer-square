#!/usr/bin/env node
import { main } from "../cli.js";
main(process.argv.slice(2), "debug-token").then((c) => process.exit(c));
