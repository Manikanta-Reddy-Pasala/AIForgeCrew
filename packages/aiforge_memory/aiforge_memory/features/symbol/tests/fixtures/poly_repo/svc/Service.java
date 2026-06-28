package com.example.svc;

import java.util.List;

public class PaymentService {
    private double fees;

    public PaymentService() {
        this.fees = 0.0;
    }

    public boolean process(double amount) {
        double n = normalize(amount);
        return n > 0;
    }

    private double normalize(double amount) {
        return Math.round(amount * 100.0) / 100.0;
    }
}
