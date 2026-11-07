/** A thin strip standing for the whole document, with a mark where the passage sits. */
export default function LocationBar({
  start,
  end,
  length,
}: {
  start: number;
  end: number;
  length: number;
}) {
  if (length <= 0) return null;
  const left = Math.min(100, (start / length) * 100);
  const width = Math.max(1, ((end - start) / length) * 100);
  const percent = Math.round(left);

  return (
    <span
      className="location"
      role="img"
      aria-label={
        percent < 5 ? "Near the start of the document" : `About ${percent}% into the document`
      }
    >
      <span
        className="location-mark"
        style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
      />
    </span>
  );
}
