type TelegramApp = {
  initData: string;
  colorScheme: 'dark' | 'light';
  ready(): void;
  expand(): void;
  onEvent(event: string, callback: () => void): void;
  offEvent(event: string, callback: () => void): void;
  BackButton: { show(): void; hide(): void; onClick(callback: () => void): void; offClick(callback: () => void): void };
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
