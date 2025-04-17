import VoiceChatInterface from "@/components/VoiceChatInterface";
import { Suspense } from "react";

export default function Home() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-between">
      {/* Using Suspense here is optional unless VoiceChatInterface itself fetches data initially */}
      {/* It's more about loading the component bundle */}
      <Suspense fallback={<div>Loading Chat Interface...</div>}>
        <VoiceChatInterface />
      </Suspense>
    </main>
  );
}