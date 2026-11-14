import type { Metadata } from "next";
import DocumentsTable from "@/components/DocumentsTable";
import SourcesPanel from "@/components/SourcesPanel";

export const metadata: Metadata = { title: "Documents | Personal Search" };

export default function DocumentsPage() {
  return (
    <main className="documents-page">
      <h1>Documents</h1>
      <p className="path">
        Everything in your docs folder and connected Google Drive accounts. New and changed files
        are picked up within a minute.
      </p>
      <SourcesPanel />
      <DocumentsTable />
    </main>
  );
}
