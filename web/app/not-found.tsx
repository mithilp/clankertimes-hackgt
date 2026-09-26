import Link from "next/link";

export default function NotFound() {
  return (
    <div className="empty">
      <h2>We couldn&apos;t find that page.</h2>
      <p>It may have moved, or it may never have existed. <Link href="/" style={{ textDecoration: "underline" }}>Go to the front page.</Link></p>
    </div>
  );
}
