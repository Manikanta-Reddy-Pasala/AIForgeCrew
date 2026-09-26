"""Which typed messages stop, or replace, the work already running.

A bare word search ended every watch on "add a Cancel button" and cut a
build on "don't forget the README". Only an imperative aimed at the run
counts."""
import pytest

from aiforge_core.runtime import run_interrupt

cut = run_interrupt.text_cuts_running_work
replaces = run_interrupt.text_replaces_work


@pytest.mark.parametrize("text", [
    "stop", "Stop!", "stop.", "STOP NOW", "stop that", "cancel it",
    "abort the run", "drop that", "kill it", "halt now", "end it",
    "terminate the run", "stop everything", "stop all watches",
    "stop watching", "stop watching the pipeline", "stop polling",
    "please stop the watch", "ok stop", "no, stop it", "can you stop it?",
    "never mind, cancel it", "cancel the scheduled run",
    "cancel the scheduled deploy for 9am", "stop the job now",
    "kill the background job", "stop the watch on the pipeline",
    "stop that, answer this instead", "Thanks. Stop the watch.",
    "stop it and write the file",
])
def test_an_imperative_aimed_at_the_run_cuts_it(text):
    assert cut(text) is True
    assert replaces(text) is True


@pytest.mark.parametrize("text", [
    "add a Cancel button",
    "Cancel button should be red",
    "the stop button is broken",
    "how do I kill the process on :3000",
    "how do I kill the process on :3000?",
    "why did the job stop?",
    "what does abort do",
    "is it possible to cancel an order",
    "can you add a halt flag",
    "kill -9 1234 is what I ran",
    "kill the process on :3000",
    "drop the users table",
    "run `kill 1234` for me",
    "type \"stop\" in the box",
    "make the button say 'Cancel'",
    "```\nstop\n```",
    "stop words should be removed from the index",
    "end-to-end tests fail",
    "add a stop() method",
    "write a function to stop the server",
    "stopping criteria are wrong",
    "don't stop",
    "don't forget the date",
    "use the API instead",
    "forget the extra log",
    "also add a log line",
    "please continue",
])
def test_the_word_alone_does_not_cut_the_run(text):
    assert cut(text) is False


@pytest.mark.parametrize("text", [
    "drop the sleep and write the file",
    "use grep instead",
    "forget the extra log",
    "scratch that",
    "never mind",
    "actually no, use sqlite",
    "skip that",
    "don't run it",
])
def test_a_change_of_plan_replaces_the_work(text):
    assert replaces(text) is True


@pytest.mark.parametrize("text", [
    "don't forget the README",
    "don't forget the date",
    "also name the function add_numbers",
    "add a Cancel button",
    "how do I kill the process on :3000",
    "please continue",
    "switch the port to 3001",
    "make sure the stop button works",
    "the note says `use this instead`",
])
def test_an_extra_detail_does_not_replace_the_work(text):
    assert replaces(text) is False
