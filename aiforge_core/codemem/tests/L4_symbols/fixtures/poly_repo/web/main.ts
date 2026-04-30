import { normalize } from "./helpers";

export class Counter {
    private count: number = 0;

    increment(): number {
        return ++this.count;
    }

    decrement(): number {
        return --this.count;
    }
}

export function bootstrap(): void {
    const c = new Counter();
    c.increment();
    console.log(normalize(c.increment()));
}
