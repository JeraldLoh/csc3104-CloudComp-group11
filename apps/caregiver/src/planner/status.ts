// The one-line message under the buttons, shared by the planner and the saved-routes list.
export interface Status {
  text: string;
  error: boolean;
}

export const info = (text: string): Status => ({ text, error: false });
export const failure = (text: string): Status => ({ text, error: true });
export const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
