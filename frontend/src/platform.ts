type TelegramApp = {
  initData: string;
  colorScheme: 'dark' | 'light';
  ready(): void;
  expand(): void;
  onEvent(event: string, callback: () => void): void;
  offEvent(event: string, callback: () => void): void;
  BackButton: { show(): void; hide(): void; onClick(callback: () => void): void; offClick(callback: () => void): void };
  isVersionAtLeast?(version: string): boolean;
  downloadFile?(params: { url: string; file_name: string }, callback?: (accepted: boolean) => void): void;
  openLink?(url: string): void;
};

declare global {
  interface Window { Telegram?: { WebApp: TelegramApp } }
}

// Platform-specific behavior stays here. Screens and API work in a browser too.
const telegram = () => window.Telegram?.WebApp;
export const initData = () => telegram()?.initData || '';
export function setupPlatform() {
  const tg = telegram();
  const theme = () => {
    document.documentElement.dataset.theme = initData() ? tg?.colorScheme :
      (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  };
  theme();
  if (initData()) {
    tg?.ready();
    tg?.expand();
    tg?.onEvent('themeChanged', theme);
  }
  return () => tg?.offEvent('themeChanged', theme);
}
export function bindBack(callback: (() => void) | null) {
  const back = telegram()?.BackButton;
  if (!initData() || !back) return () => {};
  if (!callback) { back.hide(); return () => {}; }
  back.show(); back.onClick(callback);
  return () => { back.offClick(callback); back.hide(); };
}

// Telegram's in-app browser ignores <a download>. Inside Telegram the file goes through the
// native download popup (Bot API 8.0+) or, in older apps, the external browser.
export const inTelegram = () => !!initData();
export function telegramDownload(url: string, fileName: string) {
  const tg = telegram();
  if (tg?.downloadFile && tg.isVersionAtLeast?.('8.0')) tg.downloadFile({ url, file_name: fileName });
  else if (tg?.openLink) tg.openLink(url);
  else window.location.href = url;
}
