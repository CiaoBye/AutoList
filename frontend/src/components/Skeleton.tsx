/** 读取中的占位：保持与真实内容相同的形状，数据到达时页面不跳动。 */

export function PosterSkeletons({ count, className = "film-wall" }: { count: number; className?: string }) {
  return (
    <div class={className} aria-hidden="true">
      {Array.from({ length: count }, (_, index) => (
        <span key={index} class="film-card skeleton-card">
          <span class="poster skeleton" />
          <span class="skeleton skeleton-line" />
          <span class="skeleton skeleton-line is-short" />
        </span>
      ))}
    </div>
  );
}

export function HomeSkeleton() {
  return (
    <div aria-hidden="true">
      <div class="card skeleton-hero">
        <span class="skeleton skeleton-line is-short" />
        <span class="skeleton skeleton-figure" />
        <span class="skeleton skeleton-bar" />
      </div>
      <section class="shelf">
        <span class="skeleton skeleton-line is-title" />
        <PosterSkeletons count={8} className="shelf-row" />
      </section>
    </div>
  );
}

export function DrawerSkeleton() {
  return (
    <div class="film-hero" aria-hidden="true">
      <span class="poster skeleton" />
      <span class="film-meta">
        <span class="skeleton skeleton-line is-title" />
        <span class="skeleton skeleton-line" />
        <span class="skeleton skeleton-line is-short" />
      </span>
    </div>
  );
}
