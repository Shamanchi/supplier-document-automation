import axios, {
  type AxiosError,
  type InternalAxiosRequestConfig,
} from "axios";

import {
  getAccessToken,
  notifySessionLost,
  setAccessToken,
} from "@/shared/api/auth-token";
import type { AuthSession, BackendUser } from "@/shared/api/backend-types";
import { env } from "@/shared/config/env";

declare module "axios" {
  interface InternalAxiosRequestConfig {
    _authRetry?: boolean;
  }
}

interface FastApiValidationIssue {
  loc?: unknown[];
  msg?: string;
}

export class ApiError extends Error {
  readonly status: number | null;
  readonly details: unknown;

  constructor(message: string, status: number | null, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isValidationIssue(value: unknown): value is FastApiValidationIssue {
  return isRecord(value);
}

function extractPayloadMessage(payload: unknown): string | null {
  if (!isRecord(payload)) return null;
  if (typeof payload.message === "string") return payload.message;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) {
    const messages = payload.detail.filter(isValidationIssue).map((issue) => {
      const location = Array.isArray(issue.loc)
        ? issue.loc.map(String).filter((part) => part !== "body").join(".")
        : "";
      const message = issue.msg ?? "Ошибка проверки данных";
      return location ? `${location}: ${message}` : message;
    });
    if (messages.length) return messages.join("; ");
  }
  if (isRecord(payload.detail) && typeof payload.detail.message === "string") {
    return payload.detail.message;
  }
  // Envelope ошибок модулей фазы 3 (app/core/errors.py): {error:{code,message}}.
  if (isRecord(payload.error) && typeof payload.error.message === "string") {
    return payload.error.message;
  }
  return null;
}

export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  if (axios.isAxiosError<unknown>(error)) {
    return new ApiError(
      extractPayloadMessage(error.response?.data) ||
        error.message ||
        "Не удалось выполнить запрос",
      error.response?.status ?? null,
      error.response?.data,
    );
  }
  return new ApiError(
    error instanceof Error ? error.message : "Не удалось выполнить запрос",
    null,
  );
}

export function extractApiError(error: unknown): string {
  return toApiError(error).message;
}

// Access-токен живёт в хранилище auth-token (см. файл): в памяти модуля, без
// localStorage. Тот же модуль читает SSE-подключение к событиям pipeline.
let projectId: string | null = null;

const sessionClient = axios.create({
  baseURL: env.authBaseUrl,
  timeout: 15_000,
  withCredentials: true,
});

export const apiClient = axios.create({
  baseURL: env.apiBaseUrl,
  timeout: 30_000,
  withCredentials: true,
});

export function setAuthToken(token: string | null): void {
  setAccessToken(token);
}

export function getAuthToken(): string | null {
  return getAccessToken();
}

export function setProjectId(value: string | null): void {
  projectId = value;
}

apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig): InternalAxiosRequestConfig => {
    const accessToken = getAccessToken();
    if (accessToken) {
      config.headers.set("Authorization", `Bearer ${accessToken}`);
    }
    if (projectId) config.headers.set("X-Project-ID", projectId);
    return config;
  },
);

let refreshInFlight: Promise<AuthSession> | null = null;

apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError<unknown>) => {
    const config = error.config;
    const url = config?.url ?? "";
    const isAuthRequest =
      url.includes("/auth/login") ||
      url.includes("/auth/refresh") ||
      url.includes("/auth/logout");

    if (
      error.response?.status === 401 &&
      config &&
      !config._authRetry &&
      !isAuthRequest
    ) {
      config._authRetry = true;
      try {
        refreshInFlight ??= refreshSession().finally(() => {
          refreshInFlight = null;
        });
        const session = await refreshInFlight;
        setAuthToken(session.accessToken);
        config.headers.set("Authorization", `Bearer ${session.accessToken}`);
        return await apiClient.request(config);
      } catch (refreshError) {
        setAuthToken(null);
        // Два канала потери сессии: window-событие слушает AuthProvider фазы 2,
        // notifySessionLost — подписчиков auth-token (SSE/виджеты фазы 3).
        notifySessionLost();
        window.dispatchEvent(new Event("price-loader-auth-expired"));
        return Promise.reject(toApiError(refreshError));
      }
    }

    return Promise.reject(toApiError(error));
  },
);

export async function login(
  email: string,
  password: string,
): Promise<AuthSession> {
  try {
    const { data } = await sessionClient.post<AuthSession>("/auth/login", {
      email,
      password,
    });
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

export async function refreshSession(): Promise<AuthSession> {
  try {
    const { data } = await sessionClient.post<AuthSession>("/auth/refresh");
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

export async function fetchCurrentUser(token?: string): Promise<BackendUser> {
  try {
    const { data } = await apiClient.get<BackendUser>("/auth/me", {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

export async function logoutAll(): Promise<void> {
  try {
    await sessionClient.post("/auth/logout-all");
  } catch (error) {
    throw toApiError(error);
  }
}

export async function logout(): Promise<void> {
  try {
    await sessionClient.post("/auth/logout");
  } catch (error) {
    throw toApiError(error);
  }
}

