import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { validateReportSearch } from "@/router";

/** The one-time token of a `/report` link: `/login#token=…`. */
function tokenFromHash(): string | null {
  return new URLSearchParams(window.location.hash.slice(1)).get("token");
}

export function LoginPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const login = useMutation({
    mutationFn: (value: string) => api.login(value),
    onSuccess: () => {
      queryClient.clear();
      void navigate({ to: "/", search: validateReportSearch({}) });
    },
  });

  const [token, setToken] = useState<string | null>(null);
  // Taken out of the address bar as soon as it is read, so it is not left in
  // the history.  Also on hashchange: pasting a link into a tab that already
  // shows this page changes only the fragment and loads nothing.
  useEffect(() => {
    const take = () => {
      const value = tokenFromHash();
      if (!value) return;
      window.history.replaceState(null, "", window.location.pathname);
      login.reset();
      setToken(value);
    };
    take();
    window.addEventListener("hashchange", take);
    return () => window.removeEventListener("hashchange", take);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex min-h-svh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Picnic reports</CardTitle>
          <CardDescription>
            {token ? "Your login link is ready." : "Log in with a link from the Telegram bot."}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          {token && !login.isError ? (
            // A click, not an automatic login: a link preview or prefetch
            // opening the page must not use the link up.
            <Button disabled={login.isPending} onClick={() => login.mutate(token)}>
              {login.isPending ? "Logging in…" : "Continue"}
            </Button>
          ) : (
            <>
              {login.isError && (
                <p role="alert" className="text-destructive">
                  {login.error.message}
                </p>
              )}
              <p>
                Send <code className="rounded bg-muted px-1.5 py-0.5 font-mono">/report</code> to the bot in your
                private chat and open the link it replies with.
              </p>
              <p className="text-muted-foreground">A link works once and expires after a few minutes.</p>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
