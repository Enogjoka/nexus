import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-md py-16 text-center">
      <p className="text-sm text-muted">This page does not exist.</p>
      <Link href="/" className="mt-3 inline-block text-sm text-ring2 hover:underline">
        Back to Command
      </Link>
    </div>
  );
}
