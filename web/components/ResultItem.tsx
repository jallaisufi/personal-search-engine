import { headingTrail, type SearchResult } from "@/lib/api";
import LocationBar from "./LocationBar";

const WHY: Record<string, string> = {
  "keyword,semantic": "Matches the meaning and the exact words",
  semantic: "Matches the meaning",
  keyword: "Matches the exact words",
};

export default function ResultItem({
  result,
  selected,
  onOpen,
}: {
  result: SearchResult;
  selected: boolean;
  onOpen: () => void;
}) {
  const trail = headingTrail(result.heading_path);
  const why = WHY[[...result.matched_by].sort().join(",")];

  return (
    <li className="result">
      <button
        type="button"
        className="result-open"
        aria-current={selected}
        aria-label={`Open ${result.title} at this passage`}
        onClick={onOpen}
      >
        <span className="result-title">{result.title}</span>
        <span className="result-where">
          {trail.length > 0 && (
            <span className="trail">
              {trail.map((heading, i) => (
                <span key={i}>{heading}</span>
              ))}
            </span>
          )}
          {result.page !== null && <span>Page {result.page}</span>}
          {trail.length === 0 && result.page === null && <span>{result.path}</span>}
        </span>
        <LocationBar start={result.char_start} end={result.char_end} length={result.doc_length} />
        {/* Server escapes the document text and only adds <mark> tags. */}
        <span className="result-snippet" dangerouslySetInnerHTML={{ __html: result.snippet_html }} />
        {why && <span className="result-why">{why}</span>}
      </button>
    </li>
  );
}
