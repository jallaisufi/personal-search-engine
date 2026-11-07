import type { Metadata } from "next";
import DocumentsTable from "@/components/DocumentsTable";

export const metadata: Metadata = { title: "Documents | Personal Search" };

export default function DocumentsPage() {
  return (
    <main className="documents-page">
      <h1>Documents</h1>
      <p className="path">
        Everything in your docs folder. New and changed files are picked up within a minute.
      </p>
      <DocumentsTable />
    </main>
  );
}
