# Frontend Deployment Checklist

## Pre-Deployment

### 1. Install Dependencies
```bash
cd frontend
npm install
# or
bun install
```

### 2. Environment Configuration

The frontend expects the backend API at `/api/v1`. In development, Vite proxies this to `http://localhost:8000`.

For production, configure your reverse proxy (nginx, Caddy, Apache) to proxy `/api` to the backend service.

Example nginx configuration:
```nginx
server {
    listen 80;
    server_name your-domain.com;

    # Frontend static files
    location / {
        root /var/www/frontend/dist;
        try_files $uri $uri/ /index.html;
    }

    # Backend API proxy
    location /api {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Health endpoint
    location /health {
        proxy_pass http://localhost:8000;
    }
}
```

### 3. Verify Backend is Running

Ensure the backend API is accessible at `http://localhost:8000` (or your configured endpoint).

Test health endpoint:
```bash
curl http://localhost:8000/health
```

## Development

### Start Dev Server
```bash
npm run dev
# or
bun run dev
```

Access at: [http://localhost:5173](http://localhost:5173)

### Type Checking
```bash
npm run type-check
# or
bun run type-check
```

## Production Build

### 1. Build
```bash
npm run build
# or
bun run build
```

This creates optimized production files in `frontend/dist/`.

### 2. Preview Build Locally
```bash
npm run preview
# or
bun run preview
```

### 3. Deploy Static Files

Copy `frontend/dist/` contents to your web server:

```bash
# Example: Copy to nginx root
sudo cp -r dist/* /var/www/frontend/dist/
```

## Post-Deployment Verification

### 1. Test Authentication
- Navigate to `/login`
- Dev mode: Test with username/password
- Prod mode: Test OIDC redirect

### 2. Test Core Features
- Chat: Send message, verify streaming works
- Documents: Upload document, verify processing
- Agents: View agent list
- Admin: Access admin panel (admin role only)

### 3. Test Security
- Verify JWT tokens in sessionStorage (NOT localStorage)
- Test 401 redirect on expired token
- Verify Bearer token on all API calls
- Check AI disclosure on agent messages

### 4. Browser Compatibility
Test in:
- Chrome/Edge (latest)
- Firefox (latest)
- Safari (latest)

### 5. Performance
- Check Lighthouse score (should be 90+)
- Verify bundle size (< 500KB gzipped)
- Test on slow 3G network

## Troubleshooting

### Issue: API calls fail with CORS errors
**Solution:** Ensure backend CORS middleware allows your frontend origin. Check `src/main.py` CORS configuration.

### Issue: 401 errors on all requests
**Solution:** Check JWT token format. Verify backend JWT settings match frontend expectations.

### Issue: SSE streaming doesn't work
**Solution:** Ensure reverse proxy doesn't buffer SSE responses. Add nginx config:
```nginx
proxy_buffering off;
proxy_cache off;
```

### Issue: White screen on load
**Solution:** Check browser console for errors. Verify `index.html` and `main.tsx` are correctly deployed.

### Issue: Routes return 404
**Solution:** Configure web server to serve `index.html` for all routes (SPA fallback).

## Security Checklist

- [ ] JWT tokens stored in sessionStorage only
- [ ] No sensitive data in localStorage
- [ ] Bearer token on all authenticated requests
- [ ] 401 responses trigger automatic logout
- [ ] AI disclosure on all agent messages
- [ ] Content Security Policy headers (set by backend)
- [ ] HTTPS enabled in production
- [ ] No API keys or secrets in frontend code

## Monitoring

### Recommended Metrics
- Page load time
- API response times
- Error rates (by endpoint)
- User authentication failures
- Document upload success rate

### Logging
Frontend logs are minimal by design. Server-side logging (backend) is authoritative for security events.

## Rollback Plan

If deployment fails:
1. Restore previous `dist/` directory
2. Clear browser cache
3. Verify backend compatibility
4. Check rollback logs for errors

## Support

For issues:
1. Check browser console for errors
2. Check backend logs for API errors
3. Verify network tab shows correct API calls
4. Review DEPLOYMENT.md troubleshooting section
