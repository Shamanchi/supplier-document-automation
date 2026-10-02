import { LogIn } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { useAuth } from "@/features/auth/model";
import { extractApiError } from "@/shared/api/client";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { Label } from "@/shared/ui/label";
import { LoadingState } from "@/shared/ui/page";

export function LoginPage() {
  const navigate = useNavigate();
  const { isAuthenticated, isLoading, login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  if (isLoading) return <LoadingState label="Проверка сессии..." />;
  if (isAuthenticated) return <Navigate to="/suppliers" replace />;

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setIsSubmitting(true);
    try {
      await login(email.trim(), password);
      navigate("/suppliers", { replace: true });
    } catch (loginError) {
      setError(extractApiError(loginError));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <main className="grid min-h-screen place-items-center bg-background p-5">
      <section className="w-full max-w-sm rounded-lg border border-border bg-card p-6 shadow-sm">
        <div className="mb-6">
          <div className="text-2xl font-bold text-primary">
            Пожарный<span className="text-[#F26419]">КП</span>
          </div>
          <h1 className="mt-5 text-xl font-semibold text-foreground">Вход</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Используйте учётную запись backend.
          </p>
        </div>
        <form className="grid gap-4" onSubmit={(event) => void submit(event)}>
          <div className="grid gap-2">
            <Label htmlFor="login-username">Логин</Label>
            <Input
              id="login-username"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="login-password">Пароль</Label>
            <Input
              id="login-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </div>
          {error ? (
            <p className="text-sm font-medium text-destructive" role="alert">
              {error}
            </p>
          ) : null}
          <Button type="submit" disabled={isSubmitting}>
            <LogIn />
            {isSubmitting ? "Вход..." : "Войти"}
          </Button>
        </form>
      </section>
    </main>
  );
}
