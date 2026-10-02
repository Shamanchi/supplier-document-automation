const mswSetting = import.meta.env.VITE_ENABLE_MSW;

export const env = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL || "/api/v1",
  authBaseUrl: import.meta.env.VITE_AUTH_BASE_URL || "/api/v1",
  // MSW включается только явно (`VITE_ENABLE_MSW=true`): по умолчанию все
  // запросы идут на живой бэкенд объединённого рантайма (шаги 04–05).
  // Моки остались только для контура Фазы 2 без бэкенда (checks/suppliers/
  // products/certificates/admin) — ордерные эндпоинты MSW не перехватывает.
  enableMsw: mswSetting === "true",
};
