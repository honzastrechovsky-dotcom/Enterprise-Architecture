# Enterprise Agent Platform - Frontend

React 19 + Vite + TypeScript frontend for the Enterprise Agent Platform.

## Stack

- **React 19** - UI framework
- **Vite** - Build tool and dev server
- **TypeScript** - Type safety
- **TanStack Query** - Server state management
- **React Router** - Client-side routing
- **Tailwind CSS** - Styling
- **shadcn/ui** - Component library
- **Lucide React** - Icons

## Getting Started

### Prerequisites

- Node.js 20+ or Bun 1.0+
- Backend API running at `http://localhost:8000`

### Installation

```bash
# Install dependencies
npm install
# or
bun install
```

### Development

```bash
# Start dev server (proxies /api to localhost:8000)
npm run dev
# or
bun run dev
```

Open [http://localhost:5173](http://localhost:5173) in your browser.

### Build

```bash
# Type-check and build for production
npm run build
# or
bun run build
```

### Preview

```bash
# Preview production build locally
npm run preview
# or
bun run preview
```

## Project Structure

```
frontend/
├── src/
│   ├── components/
│   │   ├── ui/              # shadcn/ui components
│   │   ├── ChatMessage.tsx  # Message display component
│   │   ├── Sidebar.tsx      # Navigation sidebar
│   │   └── DocumentUpload.tsx
│   ├── lib/
│   │   ├── api.ts           # API client with typed endpoints
│   │   ├── auth.ts          # Authentication context
│   │   └── utils.ts         # Utility functions
│   ├── pages/
│   │   ├── ChatPage.tsx     # Main chat interface
│   │   ├── DocumentsPage.tsx
│   │   ├── AgentsPage.tsx
│   │   ├── AdminPage.tsx
│   │   └── LoginPage.tsx
│   ├── types/
│   │   └── index.ts         # TypeScript type definitions
│   ├── App.tsx              # Root component with routing
│   ├── main.tsx             # Entry point
│   └── index.css            # Global styles
├── package.json
├── tsconfig.json
├── vite.config.ts
└── tailwind.config.ts
```

## Features

### Authentication
- Session-based JWT authentication (per enterprise security standard)
- Tokens stored in sessionStorage (NOT localStorage)
- Automatic 401 handling with redirect
- OIDC support for production

### Chat Interface
- Real-time streaming with SSE
- Token-by-token rendering
- Citation display with document references
- AI disclosure on all agent messages (Application Security Standard compliant)
- Conversation history

### Document Management
- Drag-and-drop upload
- Classification selector (Class I-IV)
- File type validation
- Upload progress tracking
- Document list with search/filter

### Admin Panel
- User management (role assignment, activation)
- Tenant configuration
- Audit log viewer with filters
- System health monitoring

## Security

Per enterprise security requirements:
- JWT tokens in sessionStorage only (session-scoped)
- Bearer token authentication on all API calls
- Content Security Policy headers (set by backend)
- No sensitive data in localStorage
- AI disclosure on all agent-generated content

## API Integration

All API calls use the `/api/v1` prefix, which Vite proxies to `http://localhost:8000` in development.

Production: Configure proxy in your reverse proxy (nginx, Caddy, etc.)

## Dark Theme

The application uses a dark theme by default with slate/zinc color palette and blue accents. Colors are defined in `src/index.css` using CSS variables for easy customization.

## Development Notes

- No TODOs or stubs - all code is production-ready
- TypeScript strict mode enabled
- All components follow shadcn/ui patterns
- TanStack Query for server state management
- Proper error handling on all API calls
- Loading states with skeletons
- Optimistic updates where appropriate
