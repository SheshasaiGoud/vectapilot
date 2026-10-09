import Image from "next/image";

/** A generated teammate avatar (SVG from public/avatars, background colour baked in). */
export function Avatar({
  src,
  size,
  alt = "",
  className = "",
  eager = false,
}: {
  src: string;
  size: number;
  alt?: string;
  className?: string;
  /** Load immediately — for avatars visible on first paint (the hero orbit). */
  eager?: boolean;
}) {
  return (
    <Image
      src={src}
      alt={alt}
      width={size}
      height={size}
      loading={eager ? "eager" : "lazy"}
      draggable={false}
      className={`block h-auto w-full select-none ${className}`}
    />
  );
}
