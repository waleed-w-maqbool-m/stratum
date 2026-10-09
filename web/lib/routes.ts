import { DEMO } from "./demo";

/**
 * The doors every page offers. On the public demo they open Pagila directly. On a live
 * engine a database has to be connected before it can be opened, so they go through
 * Connect, which lists the benchmark databases and takes your own.
 */
export const MAP = DEMO ? "/db/pagila_legacy/atlas" : "/connect";
export const ASK = DEMO ? "/db/pagila_legacy/ask" : "/connect";
export const OWN = DEMO ? "/schema" : "/connect";
