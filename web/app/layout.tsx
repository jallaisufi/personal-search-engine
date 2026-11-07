import type { Metadata } from "next";
import { IBM_Plex_Sans, Literata } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-sans",
  display: "swap",
});

// Document text is set in a reading serif, so passages look like the documents they came from.
const serif = Literata({
  subsets: ["latin"],
  variable: "--font-serif",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Personal Search",
  description: "Find where you read something, across your own documents.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${serif.variable}`}>
      <body>
        <header className="site-header">
          <Link href="/" className="wordmark">
            Personal Search
          </Link>
          <nav aria-label="Main">
            <Link href="/">Search</Link>
            <Link href="/documents">Documents</Link>
          </nav>
        </header>
        {children}
      </body>
    </html>
  );
}
