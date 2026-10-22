import "./BrandIcon.css";

type BrandIconProps = {active?: boolean; size?: number};
export default function BrandIcon({active=false,size=52}:BrandIconProps) {
  return <span className={"knowledge-mark"+(active?" is-active":"")} style={{width:size,height:size,flexBasis:size}} aria-hidden="true" data-active={active}>
    <svg viewBox="0 0 88 88" fill="none" focusable="false">
      <g className="knowledge-mark__rings" stroke="currentColor" strokeWidth="3.8" strokeLinecap="round">
        <g className="knowledge-mark__ring knowledge-mark__ring--outer">
          <circle cx="44" cy="44" r="35" pathLength="100" strokeDasharray="24 6 18 7 18 8 13 6" transform="rotate(-132 44 44)"/>
          <path d="M44 9v7"/>
        </g>
        <g className="knowledge-mark__ring knowledge-mark__ring--middle">
          <circle cx="44" cy="44" r="28" pathLength="100" strokeDasharray="31 7 18 7 30 7" transform="rotate(-91 44 44)"/>
          <path d="m24.2 63.8-4.9 4.9"/>
        </g>
        <g className="knowledge-mark__ring knowledge-mark__ring--inner">
          <circle cx="44" cy="44" r="21" pathLength="100" strokeDasharray="35 8 27 8 14 8" transform="rotate(-62 44 44)"/>
          <path d="M65 44h7"/>
        </g>
        <g className="knowledge-mark__ring knowledge-mark__ring--heart">
          <circle cx="44" cy="44" r="14" pathLength="100" strokeDasharray="69 12 11 8" transform="rotate(-47 44 44)"/>
          <circle cx="44" cy="44" r="7" pathLength="100" strokeDasharray="76 24" transform="rotate(-75 44 44)"/>
        </g>
      </g>
      <circle className="knowledge-mark__center" cx="44" cy="44" r="2.8" fill="currentColor"/>
    </svg>
  </span>;
}
