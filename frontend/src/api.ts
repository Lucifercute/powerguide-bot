export type Summary = { id: number; title: string; status: string; origin: string; created_at: string };
export type Project = Summary & {
  revision: number; stage: string; description: string; note: string;
  answers: Record<string, string>; questions: { key: string; text: string }[];
  sources: { name: string }[]; notes: string[];
  slides: { number: number; text: string; missing: string[]; image: string | null; caption: string | null }[];
  job: { id: number; action: string; status: string; progress: string; error: string | null } | null;
  busy: boolean; actions: string[]; backgrounds: { choice: string; url: string }[];
  can_change_background: boolean; reference: string | null; mode: string | null;
  files: { kind: string; label: string; url: string }[];
  previews: { number: number; url: string }[]; html_preview: string | null; text_report: string;
};
export type Config = { dev_login: boolean; modes: string[]; max_slides: number; daily_limit: number };
export type Me = { user_id: number; is_dev: boolean; is_guest?: boolean; name?: string; used_today: number; daily_limit: number };
export type Preset = { number: number; preview: string };
export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch('/api/v1' + path, { ...options, headers, credentials: 'same-origin' });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, typeof body.detail === 'string' ? body.detail : 'Не удалось выполнить действие. Проверьте данные и повторите.');
  }
  return response.status === 204 ? undefined as T : response.json();
}
export const statusLabel = (status: string) => ({ in_progress: 'В работе', completed: 'Готово', cancelled: 'Отменена' }[status] || status);
