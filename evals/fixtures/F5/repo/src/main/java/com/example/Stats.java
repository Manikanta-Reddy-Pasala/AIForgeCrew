package com.example;

public class Stats {
    public static double average(int[] arr) {
        if (arr == null || arr.length == 0) return 0.0;
        long s = 0;
        for (int i = 0; i < arr.length; i++) {
            s += arr[i];
        }
        return (double) s / arr.length;
    }
}
