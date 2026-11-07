import { Suspense } from "react";
import SearchApp from "@/components/SearchApp";

export default function Home() {
  return (
    <main className="search-page">
      {/* useSearchParams needs a Suspense boundary for static rendering */}
      <Suspense>
        <SearchApp />
      </Suspense>
    </main>
  );
}
