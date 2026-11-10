// Intended JS counterpart, declarations only. NOT implemented/enabled before the Python gate.
// Same JSON fields/defaults/errors as 499-flat-schemas.json; unknown keys fail at the broker.
// No connection, raw request, page evaluation, policy or nested runner method.
export type TabId = string;
export type Target = {ref: string} | {selector: string};
export type Modifier = "Shift" | "Control" | "Alt" | "Meta";
export type QueryField = "ref" | "text" | "href" | "src" | "name" | "value" | "checked" | "type" | "content" | "role" | "label";
export type Condition =
  | {kind: "text" | "text_gone" | "url" | "title"; value: string}
  | {kind: "target"; target: Target}
  | {kind: "network_idle"};
export interface Result {
  ok: boolean;
  call: string;
  operation: string;
  tab_id?: TabId;
  outcome: "not_dispatched" | "dispatched" | "observed" | "unknown";
  data: Record<string, unknown>;
  truncated: boolean;
  error?: {code: string; message: string; retryable: boolean; hint: string; state: Record<string, unknown>};
}
export declare class BrowserOperationError extends Error { result: Result; }
export interface Browser {
  tabs(): Promise<Result>;
  new_tab(options?: {url?: string; timeout_ms?: number}): Promise<Result>;
  close_tab(options: {tab_id: TabId; timeout_ms?: number}): Promise<Result>;
  navigate(options: {tab_id: TabId; url: string; timeout_ms?: number}): Promise<Result>;
  back(options: {tab_id: TabId; timeout_ms?: number}): Promise<Result>;
  forward(options: {tab_id: TabId; timeout_ms?: number}): Promise<Result>;
  reload(options: {tab_id: TabId; timeout_ms?: number}): Promise<Result>;
  read(options: {tab_id: TabId; format?: "snapshot" | "text"; target?: Target; max_chars?: number; timeout_ms?: number}): Promise<Result>;
  query(options: {tab_id: TabId; selector: string; fields?: QueryField[]; limit?: number; timeout_ms?: number}): Promise<Result>;
  find(options: {tab_id: TabId; text: string; limit?: number; timeout_ms?: number}): Promise<Result>;
  click(options: {tab_id: TabId; target: Target; button?: "left" | "middle" | "right"; click_count?: 1 | 2; modifiers?: Modifier[]; timeout_ms?: number}): Promise<Result>;
  input(options: {tab_id: TabId; target: Target; text: string; mode?: "replace" | "type"; timeout_ms?: number}): Promise<Result>;
  select(options: {tab_id: TabId; target: Target; values: string[]; by?: "value" | "label"; timeout_ms?: number}): Promise<Result>;
  check(options: {tab_id: TabId; target: Target; checked?: boolean; timeout_ms?: number}): Promise<Result>;
  press(options: {tab_id: TabId; target: Target; key: string; modifiers?: Modifier[]; timeout_ms?: number}): Promise<Result>;
  hover(options: {tab_id: TabId; target: Target; timeout_ms?: number}): Promise<Result>;
  drag(options: {tab_id: TabId; source: Target; destination: Target; timeout_ms?: number}): Promise<Result>;
  scroll(options: {tab_id: TabId; direction?: "up" | "down" | "left" | "right" | "top" | "bottom"; amount_px?: number; target?: Target; timeout_ms?: number}): Promise<Result>;
  wait(options: {tab_id: TabId; condition: Condition; timeout_ms?: number}): Promise<Result>;
  screenshot(options: {tab_id: TabId; scope?: "viewport" | "element" | "full_page"; target?: Target; timeout_ms?: number}): Promise<Result>;
  request_tab(options: {tab_id: TabId; timeout_ms?: number}): Promise<Result>;
  ask_user(options: {reason: "sign_in" | "human_check" | "trusted_input" | "other"; message: string; tab_id?: TabId; timeout_ms?: number}): Promise<Result>;
  resume(options: {call: string; resume_token: string; wait_ms?: number}): Promise<Result>;
}
export declare const browser: Browser;
export declare function emit(value: unknown): void; // JSON-compatible selected output only.
export declare function emit_image(handle: string): void; // A screenshot handle, not a path/base64 string.
