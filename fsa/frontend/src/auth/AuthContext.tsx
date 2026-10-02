import axios from 'axios'
import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { setAuthToken } from '../api/client'
export interface AuthUser { id: string; email: string; displayName: string; roles: string[]; role: string }
interface AuthContextValue { token: string | null; user: AuthUser | null; isLoading: boolean; login: (email: string, password: string) => Promise<void>; logout: () => Promise<void> }
interface SessionResponse { accessToken: string }
const AuthContext = createContext<AuthContextValue | null>(null)
const authApi = axios.create({ baseURL: '/api/v1', withCredentials: true })
authApi.interceptors.request.use((config) => {
  const token = getAuthToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

let authToken: string | null = null
function getAuthToken() { return authToken }
function applyAuthToken(token: string | null) { authToken = token; setAuthToken(token) }
export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null)
  const [user, setUser] = useState<AuthUser | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const loadUser = async () => (await authApi.get<AuthUser>('/auth/me')).data
  useEffect(() => { authApi.post<SessionResponse>('/auth/refresh').then(async ({ data }) => { applyAuthToken(data.accessToken); setToken(data.accessToken); setUser(await loadUser()) }).catch(() => { applyAuthToken(null); setToken(null); setUser(null) }).finally(() => setIsLoading(false)) }, [])
  useEffect(() => { const expire = () => { applyAuthToken(null); setToken(null); setUser(null) }; window.addEventListener('fsa-auth-expired', expire); return () => window.removeEventListener('fsa-auth-expired', expire) }, [])
  const value = useMemo<AuthContextValue>(() => ({ token, user, isLoading, login: async (email, password) => { const { data } = await authApi.post<SessionResponse>('/auth/login', { email, password }); applyAuthToken(data.accessToken); setToken(data.accessToken); setUser(await loadUser()) }, logout: async () => { await authApi.post('/auth/logout').catch(() => undefined); applyAuthToken(null); setToken(null); setUser(null) } }), [token, user, isLoading])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
export function useAuth(): AuthContextValue { const value = useContext(AuthContext); if (!value) throw new Error('useAuth must be used inside AuthProvider'); return value }
