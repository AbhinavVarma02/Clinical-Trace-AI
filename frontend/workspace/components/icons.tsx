// Minimal stroke icon set (24px grid, currentColor). Decorative: aria-hidden by default.
import type { ReactNode, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Icon({ size = 18, children, ...rest }: IconProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const Icons = {
  overview: (p: IconProps) => (
    <Icon {...p}>
      <rect x="3" y="3" width="7" height="9" rx="1.5" />
      <rect x="14" y="3" width="7" height="5" rx="1.5" />
      <rect x="14" y="12" width="7" height="9" rx="1.5" />
      <rect x="3" y="16" width="7" height="5" rx="1.5" />
    </Icon>
  ),
  patients: (p: IconProps) => (
    <Icon {...p}>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5" />
      <path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14.8c2 .7 3.2 2.4 3.6 5.2" />
    </Icon>
  ),
  agent: (p: IconProps) => (
    <Icon {...p}>
      <path d="M4 5h16v10H9l-5 4z" />
      <path d="M8.5 9.5h7M8.5 12h4" />
    </Icon>
  ),
  risk: (p: IconProps) => (
    <Icon {...p}>
      <path d="M4 19V5M4 19h16" />
      <path d="M8 15l3.5-4 3 2.5L20 7" />
    </Icon>
  ),
  evidence: (p: IconProps) => (
    <Icon {...p}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M20 20l-4.2-4.2M8.5 11h5M11 8.5v5" />
    </Icon>
  ),
  evaluation: (p: IconProps) => (
    <Icon {...p}>
      <path d="M9 3h6l1 3H8z" />
      <rect x="5" y="5" width="14" height="16" rx="2" />
      <path d="M9 13l2 2 4-4.5" />
    </Icon>
  ),
  operations: (p: IconProps) => (
    <Icon {...p}>
      <path d="M3 12h4l3-7 4 14 3-7h4" />
    </Icon>
  ),
  governance: (p: IconProps) => (
    <Icon {...p}>
      <path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z" />
      <path d="M8.8 12l2.2 2.2 4.4-4.4" />
    </Icon>
  ),
  sun: (p: IconProps) => (
    <Icon {...p}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </Icon>
  ),
  moon: (p: IconProps) => (
    <Icon {...p}>
      <path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z" />
    </Icon>
  ),
  menu: (p: IconProps) => (
    <Icon {...p}>
      <path d="M4 7h16M4 12h16M4 17h16" />
    </Icon>
  ),
  close: (p: IconProps) => (
    <Icon {...p}>
      <path d="M6 6l12 12M18 6L6 18" />
    </Icon>
  ),
  send: (p: IconProps) => (
    <Icon {...p}>
      <path d="M4 12l16-8-6 16-2.5-6.5z" />
    </Icon>
  ),
  play: (p: IconProps) => (
    <Icon {...p}>
      <path d="M7 5l12 7-12 7z" />
    </Icon>
  ),
  file: (p: IconProps) => (
    <Icon {...p}>
      <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <path d="M14 3v6h6M8 13h8M8 17h5" />
    </Icon>
  ),
  alert: (p: IconProps) => (
    <Icon {...p}>
      <path d="M12 3l9.5 17h-19z" />
      <path d="M12 10v4.5M12 17.5v.5" />
    </Icon>
  ),
  check: (p: IconProps) => (
    <Icon {...p}>
      <path d="M5 12.5l4.5 4.5L19 7.5" />
    </Icon>
  ),
  refresh: (p: IconProps) => (
    <Icon {...p}>
      <path d="M20 11a8 8 0 0 0-14.5-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.5 4.5L20 16M20 20v-4h-4" />
    </Icon>
  ),
  arrow: (p: IconProps) => (
    <Icon {...p}>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </Icon>
  ),
};
