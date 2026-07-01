import type { RunEvent } from "./events";
declare function validate(value: unknown): value is RunEvent;
export = validate;
