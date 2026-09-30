"use client";

import { DoctrineCard, LatestEventsCard, PerfCard, PositionsCard } from "@/components/CommandCards";

export default function CommandPage() {
  return (
    <div className="mx-auto grid max-w-7xl gap-3 lg:grid-cols-12">
      <DoctrineCard className="lg:col-span-5" />
      <PerfCard className="lg:col-span-7" />
      <PositionsCard className="lg:col-span-7" />
      <LatestEventsCard className="lg:col-span-5" />
    </div>
  );
}
