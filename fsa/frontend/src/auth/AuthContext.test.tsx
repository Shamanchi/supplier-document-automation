import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { AuthProvider, useAuth } from './AuthContext'

const { get } = vi.hoisted(() => ({ get: vi.fn().mockResolvedValue({ data: { id: '1', email: 'admin@example.local', displayName: 'Admin', roles: ['sysadmin'], role: 'sysadmin' } }) }))
vi.mock('axios', () => ({ default: { create: () => ({ get, post: vi.fn().mockResolvedValue({ data: { accessToken: 'jwt' } }), interceptors: { request: { use: vi.fn() } } }) } }))
vi.mock('../api/client', () => ({ setAuthToken: vi.fn() }))

function Probe() { const { token, user, login } = useAuth(); return <><span data-testid="token">{token ?? 'none'}</span><span data-testid="role">{user?.role ?? 'none'}</span><button onClick={() => void login('admin@example.local', 'secret')}>login</button></> }

describe('AuthContext', () => {
  it('logs in with shared credentials and validates FSA access', async () => {
    render(<AuthProvider><Probe /></AuthProvider>)
    await userEvent.click(screen.getByText('login'))
    expect(await screen.findByTestId('token')).toHaveTextContent('jwt')
    expect(await screen.findByTestId('role')).toHaveTextContent('sysadmin')
    expect(get).toHaveBeenCalledWith('/auth/me')
  })
})
