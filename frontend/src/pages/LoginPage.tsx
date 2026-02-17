/**
 * LoginPage - Authentication entry point.
 *
 * Dev mode: Username/password form (symmetric JWT)
 * Prod mode: OIDC redirect button
 */

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { LogIn } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '@/components/ui/card'
import { useAuth } from '@/lib/auth'

const IS_DEV = import.meta.env.DEV

export function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const { login } = useAuth()
  const navigate = useNavigate()

  const handleDevLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError('')

    try {
      // Dev mode: Simple JWT authentication
      const response = await fetch('/api/v1/auth/dev-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })

      if (!response.ok) {
        const err = await response.json()
        throw new Error(err.detail || 'Login failed')
      }

      const data = await response.json()
      login(data.access_token, data.user)
      navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  const handleOIDCLogin = () => {
    // Production: Redirect to OIDC provider
    window.location.href = '/api/v1/auth/login'
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="text-center">
          <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-primary/10 flex items-center justify-center">
            <LogIn className="w-8 h-8 text-primary" />
          </div>
          <CardTitle className="text-2xl">Enterprise Agent Platform</CardTitle>
          <CardDescription>
            {IS_DEV
              ? 'Sign in with your credentials'
              : 'Sign in with your organization account'}
          </CardDescription>
        </CardHeader>

        <CardContent>
          {IS_DEV ? (
            <form onSubmit={handleDevLogin} className="space-y-4">
              <div>
                <label className="text-sm font-medium mb-2 block">
                  Username
                </label>
                <Input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="Enter username"
                  required
                  disabled={loading}
                />
              </div>

              <div>
                <label className="text-sm font-medium mb-2 block">
                  Password
                </label>
                <Input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Enter password"
                  required
                  disabled={loading}
                />
              </div>

              {error && (
                <div className="p-3 rounded-md bg-destructive/10 text-destructive text-sm">
                  {error}
                </div>
              )}

              <Button type="submit" className="w-full" disabled={loading}>
                {loading ? 'Signing in...' : 'Sign In'}
              </Button>

              <p className="text-xs text-muted-foreground text-center">
                Development mode - Using symmetric JWT
              </p>
            </form>
          ) : (
            <div className="space-y-4">
              <Button onClick={handleOIDCLogin} className="w-full" size="lg">
                Sign in with SSO
              </Button>
              <p className="text-xs text-muted-foreground text-center">
                You will be redirected to your organization's identity provider
              </p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
