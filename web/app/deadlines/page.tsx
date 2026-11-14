import type { Metadata } from "next";
import DeadlinesList from "@/components/DeadlinesList";

export const metadata: Metadata = { title: "Deadlines | Personal Search" };

export default function DeadlinesPage() {
  return (
    <main className="search-page">
      <h1 className="page-title">Deadlines</h1>
      <p className="path">
        Dates in your documents by which something has to be done, soonest first. Open one to see
        the sentence it comes from.
      </p>
      <DeadlinesList />
    </main>
  );
}
