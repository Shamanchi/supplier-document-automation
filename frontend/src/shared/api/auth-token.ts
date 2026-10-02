/**
 * Хранилище access-токена.
 *
 * Токен держится только в памяти модуля: localStorage доступен любому скрипту
 * на странице, поэтому при XSS утекал бы вместе с сессией. Refresh-токен
 * хранится браузером в httpOnly cookie и коду недоступен вовсе — после
 * перезагрузки страницы сессия восстанавливается запросом `POST /auth/refresh`.
 */

let accessToken: string | null = null;

/** Подписчики на потерю сессии — им нужно увести пользователя на вход. */
const sessionLostHandlers = new Set<() => void>();

export function getAccessToken() {
  return accessToken;
}

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function onSessionLost(handler: () => void) {
  sessionLostHandlers.add(handler);
  return () => sessionLostHandlers.delete(handler);
}

export function notifySessionLost() {
  accessToken = null;
  sessionLostHandlers.forEach((handler) => handler());
}
