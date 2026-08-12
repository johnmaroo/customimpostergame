import { createGame, joinGame } from "@/app/actions";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function HomePage() {
  return (
    <main className="mx-auto flex min-h-full w-full max-w-md flex-col gap-6 px-4 py-10">
      <div className="space-y-2 text-center">
        <p className="text-sm font-medium tracking-[0.2em] text-muted-foreground uppercase">
          Phone party game
        </p>
        <h1 className="text-4xl font-semibold tracking-tight">Imposter</h1>
        <p className="text-muted-foreground text-base text-pretty">
          Everyone looks at their own phone. Most people get a secret word. Imposters get a
          category clue instead.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Host a game</CardTitle>
          <CardDescription>Create a room and share the code with the group.</CardDescription>
        </CardHeader>
        <CardContent>
          <form action={createGame} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="host-name">Your name</Label>
              <Input id="host-name" name="name" autoComplete="given-name" required className="h-12" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="imposters">Imposters</Label>
              <Input
                id="imposters"
                name="imposters"
                type="number"
                min={1}
                defaultValue={1}
                required
                className="h-12"
              />
            </div>
            <Button type="submit" className="h-12 w-full text-base">
              Create room
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Join</CardTitle>
          <CardDescription>Enter the 4-letter code from the host.</CardDescription>
        </CardHeader>
        <CardContent>
          <form action={joinGame} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="join-name">Your name</Label>
              <Input id="join-name" name="name" autoComplete="given-name" required className="h-12" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="code">Room code</Label>
              <Input
                id="code"
                name="code"
                autoCapitalize="characters"
                autoCorrect="off"
                spellCheck={false}
                required
                className="h-12 tracking-[0.3em] uppercase"
              />
            </div>
            <Button type="submit" variant="secondary" className="h-12 w-full text-base">
              Join room
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
