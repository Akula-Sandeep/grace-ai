function FactList({ items, empty = 'No supporting detail available.' }) {
  const values = Array.isArray(items) ? items : items ? [items] : [];
  const points = values.flatMap((item) => (
    Array.isArray(item) ? item : String(item || '').split(/\r?\n/)
  )).map((item) => String(item).trim()).filter(Boolean);

  return points.length ? (
    <ul className="fact-list">
      {points.map((point, index) => <li key={`${point}-${index}`}>{point}</li>)}
    </ul>
  ) : <p className="muted-note">{empty}</p>;
}

export default FactList;
