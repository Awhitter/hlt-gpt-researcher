// <hlt-app-switcher> is Katailyst's cross-app switcher, a web component loaded by app/layout.tsx
// from https://katailyst2.vercel.app/hlt-switcher.js. Its attributes are plain strings.
import "react";

declare module "react" {
  namespace JSX {
    interface IntrinsicElements {
      "hlt-app-switcher": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & {
        app?: string;
        theme?: "dark" | "light";
        variant?: "ghost";
        placement?: "floating";
      };
    }
  }
}
