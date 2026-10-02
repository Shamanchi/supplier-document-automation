import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthProvider, useAuth } from './AuthContext';

const api = vi.hoisted(() => ({
  login: vi.fn(),
  logout: vi.fn(),
  refreshSession: vi.fn(),
  setAuthToken: vi.fn(),
}));

vi.mock('../api/client', () => api);

function Probe() {
  const { user, isLoading } = useAuth();
  return <div>{isLoading ? 'loading' : user?.username ?? 'anonymous'}</div>;
}

describe('AuthProvider startup', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('attempts the shared refresh cookie on every visit', async () => {
    api.refreshSession.mockRejectedValue(new Error('no session'));
    render(<AuthProvider><Probe /></AuthProvider>);

    expect(await screen.findByText('anonymous')).toBeInTheDocument();
    expect(api.refreshSession).toHaveBeenCalledTimes(1);
  });

  it('restores a sysadmin session from the shared backend', async () => {
    api.refreshSession.mockResolvedValue({
      accessToken: 'access',
      expiresIn: 900,
      user: {
        id: '1', email: 'sysadmin@example.local', display_name: 'Sysadmin', initials: 'S',
        username: 'sysadmin@example.local', role: 'admin', roles: ['sysadmin'], permissions: {},
      },
    });

    render(<AuthProvider><Probe /></AuthProvider>);

    expect(await screen.findByText('sysadmin@example.local')).toBeInTheDocument();
    expect(api.refreshSession).toHaveBeenCalledTimes(1);
    expect(api.setAuthToken).toHaveBeenCalledWith('access');
  });
});
