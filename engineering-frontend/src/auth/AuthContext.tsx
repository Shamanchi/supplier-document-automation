import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  login as loginRequest,
  logout as logoutRequest,
  refreshSession,
  setAuthToken,
} from '../api/client';
import type { AuthUser } from '../api/client';

interface AuthContextValue {
  user: AuthUser | null;
  isAdmin: boolean;
  isSysadmin: boolean;
  isLoading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // Канон identity фазы 3 (merge-remote, шаг 06b): составная сессия от
  // client.ts — camelCase-токен + пользователь из GET /auth/me.
  const applySession = (session: Awaited<ReturnType<typeof refreshSession>>) => {
    setAuthToken(session.accessToken);
    setUser(session.user);
  };

  const clearSession = () => {
    setAuthToken(null);
    setUser(null);
  };

  useEffect(() => {
    refreshSession()
      .then(applySession)
      .catch(clearSession)
      .finally(() => setIsLoading(false));
  }, []);

  useEffect(() => {
    const expire = () => clearSession();
    window.addEventListener('price-loader-auth-expired', expire);
    return () => window.removeEventListener('price-loader-auth-expired', expire);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isAdmin: Boolean(user?.roles.some((role) => role === 'admin' || role === 'sysadmin')),
      isSysadmin: Boolean(user?.roles.includes('sysadmin')),
      isLoading,
      login: async (username, password) => applySession(await loginRequest(username, password)),
      logout: async () => {
        try {
          await logoutRequest();
        } finally {
          clearSession();
        }
      },
    }),
    [user, isLoading],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}
