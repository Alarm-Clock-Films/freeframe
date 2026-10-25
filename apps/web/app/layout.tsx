import type { Metadata, Viewport } from "next";
import { DM_Sans } from "next/font/google";
import { ToastProvider } from "@/components/shared/toast";
import { ThemeInitializer } from "@/components/shared/theme-initializer";
import { BrandingHead } from "@/components/shared/branding-head";
import { BrandingProvider } from "@/components/shared/branding-provider";
import { getServerBranding } from "@/lib/branding-server";
import { accentVars, ACCENT_CACHE_KEY } from "@/lib/accent";
import { withBasePath } from "@/lib/base-path";
import { vercelInsightsEnabled } from "@/lib/insights";
import { VercelInsights } from "@/components/shared/vercel-insights";
import { logoUrlFromApi } from "@/lib/branding-logo";
import "./globals.css";

const dmSans = DM_Sans({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-sans",
  weight: ["400", "500", "600", "700"],
  preload: true,
});

// The tab title comes from the instance's own name, resolved on the server, so
// a white-labelled deployment never shows "FreeFrame" in the tab first and the
// org name a round trip later. `icons` is deliberately NOT set here: Next emits
// React-owned icon links for a `metadata.icons` entry, and BrandingHead's sweep
// removes competing icon links with `el.remove()`, which throws when the node
// belongs to React. The icon links are rendered in `<head>` below instead,
// carrying the `data-ff-branding` attribute that sweep is written to skip.
/** `<meta name="robots" content="noindex, nofollow">`, on every page. */
const NOINDEX: Metadata["robots"] = { index: false, follow: false };

export async function generateMetadata(): Promise<Metadata> {
  const branding = await getServerBranding();
  return {
    title: branding?.org_name || "FreeFrame",
    description: "Collaborative media review and approval platform",
    // Keep every page out of search results (see app/robots.ts). Child
    // layouts and pages inherit this unless they set `robots` themselves.
    robots: NOINDEX,
  };
}

// Render every page per request rather than once at build. The branding read
// below was the only data this layout needs, and with nothing else marking the
// routes dynamic Next prerendered them at `next build` -- where no API is
// listening, so the HTML was baked with the FreeFrame defaults. Every visitor
// then saw the FreeFrame logo, name and accent until the browser's own fetch
// replaced them. (ISR regeneration did not save it: it serves the stale page
// first, and a page left idle could carry logo URLs whose signatures expired.)
// The branding fetch itself is still cached for 60s, so this costs no extra
// round trip to the API per page view.
export const dynamic = "force-dynamic";

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#0A0A0B",
  // Lets the layout use the full display on a notched phone, and — the reason
  // it is here — makes env(safe-area-inset-*) resolve to real values. Without
  // it iOS reports 0 for all of them, so any padding written against them is
  // inert. Every surface that pins content to an edge is padded with the
  // `*-safe` utilities in globals.css; adding `cover` without those would push
  // content under the notch and the home indicator instead.
  viewportFit: "cover",
};

/** Shown when an admin hasn't set one — the same defaults BrandingHead falls back to. */
const DEFAULT_FAVICON = "/logo-icon.png";
const DEFAULT_APPLE_ICON = "/apple-icon.png";

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const branding = await getServerBranding();
  const favicon = logoUrlFromApi(branding?.favicon_url) || withBasePath(DEFAULT_FAVICON);
  const appleIcon = logoUrlFromApi(branding?.apple_icon_url) || withBasePath(DEFAULT_APPLE_ICON);
  // Painting the accent from the server too, so a branded instance does not
  // render one frame in the product's default purple before the store lands.
  const accent = accentVars(branding?.primary_color ?? null);

  return (
    <html
      lang="en"
      suppressHydrationWarning
      style={accent as React.CSSProperties | undefined}
      data-ff-server-branding={branding ? "" : undefined}
    >
      <head>
        {/* Inline script to apply theme BEFORE paint — prevents flash */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var d=JSON.parse(localStorage.getItem('ff-theme')||'{}');var t=d.state&&d.state.theme||'dark';if(t==='system'){t=window.matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'}document.documentElement.setAttribute('data-theme',t)}catch(e){document.documentElement.setAttribute('data-theme','dark')}})()`,
          }}
        />
        {/* When the server could not read branding, paint the accent this
            browser saved last visit (ACCENT_CACHE_KEY) before
            first paint, instead of the stock colour until the fetch lands.
            Skipped when the server did supply branding: its accent, or its
            lack of one, is already on <html> and is the current truth. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var r=document.documentElement;if(r.hasAttribute('data-ff-server-branding'))return;var v=JSON.parse(localStorage.getItem(${JSON.stringify(ACCENT_CACHE_KEY)})||'null');if(v&&typeof v==='object')for(var k in v)if(k.indexOf('--')===0)r.style.setProperty(k,String(v[k]))}catch(e){}})()`,
          }}
        />
        {/* The icon the browser shows while the page is still loading. Carries
            `data-ff-branding` so BrandingHead adopts these rather than sweeping
            them away, and updates the href in place if branding changes. */}
        <link rel="icon" href={favicon} data-ff-branding="" />
        <link rel="shortcut icon" href={favicon} data-ff-branding="" />
        <link rel="apple-touch-icon" href={appleIcon} data-ff-branding="" />
      </head>
      <body className={`${dmSans.variable} font-sans antialiased`}>
        <ThemeInitializer />
        <BrandingProvider initial={branding}>
          {/* Renders nothing; it keeps the live document in step with branding
              after a change. It sits inside the provider rather than in <head>
              so it reads the server's values on the first pass and leaves the
              title and icons rendered above alone, instead of resetting them to
              the defaults and undoing the very flash this removes. */}
          <BrandingHead />
          <ToastProvider>{children}</ToastProvider>
        </BrandingProvider>
        {/* Vercel Web Analytics and Speed Insights, on Vercel only (lib/insights.ts). */}
        {vercelInsightsEnabled() && <VercelInsights />}
      </body>
    </html>
  );
}
