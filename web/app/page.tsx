import { Landing } from "@/components/landing/Landing";

/**
 * The landing page. Everything about it lives in the Landing component: the Atlas as
 * the first screen, three short beats driven by scroll, and three ways in. The detail
 * that used to run down this page now lives at /how-it-works.
 */
export default function Home() {
  return <Landing />;
}
