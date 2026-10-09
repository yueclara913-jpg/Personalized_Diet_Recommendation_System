// DOM Elements
document.addEventListener('DOMContentLoaded', function() {
    // Tab switching
    const tabButtons = document.querySelectorAll('.tab-button');
    const tabContents = document.querySelectorAll('.tab-content');
    
    // Form elements
    const ageInput = document.getElementById('age');
    const genderSelect = document.getElementById('gender');
    const weightInput = document.getElementById('weight');
    const heightInput = document.getElementById('height');
    const bmiInput = document.getElementById('bmi');
    const activityLevelSelect = document.getElementById('activity-level');
    const goalSelect = document.getElementById('goal');
    const vegetarianCheckbox = document.getElementById('vegetarian');
    const veganCheckbox = document.getElementById('vegan');
    const lowCarbCheckbox = document.getElementById('low-carb');
    const lowFatCheckbox = document.getElementById('low-fat');
    const highProteinCheckbox = document.getElementById('high-protein');
    const allergiesInput = document.getElementById('allergies');
    
    // Buttons
    const calculateBmiButton = document.getElementById('calculate-bmi');
    const generateRecommendationsButton = document.getElementById('generate-recommendations');
    const viewVisualizationButton = document.getElementById('view-visualization');
    
    // Loading and error elements
    const loadingOverlay = document.getElementById('loading-overlay');
    const errorModal = document.getElementById('error-modal');
    const errorMessage = document.getElementById('error-message');
    const closeButton = document.querySelector('.close-button');
    
    // Chart objects
    let macrosChart = null;
    let mealCaloriesChart = null;
    let nutrientComparisonChart = null;
    
    // Set up event listeners
    setupEventListeners();
    
    // Initialize
    function setupEventListeners() {
        // Tab switching
        tabButtons.forEach(button => {
            button.addEventListener('click', () => switchTab(button.dataset.tab));
        });
        
        // BMI calculation
        calculateBmiButton.addEventListener('click', calculateBMI);
        
        // Auto calculate BMI when weight and height are entered
        weightInput.addEventListener('change', calculateBMI);
        heightInput.addEventListener('change', calculateBMI);
        
        // Generate recommendations
        generateRecommendationsButton.addEventListener('click', generateRecommendations);
        
        // View visualization
        viewVisualizationButton.addEventListener('click', () => switchTab('visualization'));
        
        // Vegetarian/Vegan dependency
        veganCheckbox.addEventListener('change', function() {
            if (this.checked) {
                vegetarianCheckbox.checked = true;
            }
        });
        
        vegetarianCheckbox.addEventListener('change', function() {
            if (!this.checked) {
                veganCheckbox.checked = false;
            }
        });
        
        // Close error modal
        closeButton.addEventListener('click', () => errorModal.style.display = 'none');
        window.addEventListener('click', (e) => {
            if (e.target === errorModal) {
                errorModal.style.display = 'none';
            }
        });
    }
    
    // Switch between tabs
    function switchTab(tabId) {
        // Update button active states
        tabButtons.forEach(button => {
            button.classList.toggle('active', button.dataset.tab === tabId);
        });
        
        // Show selected tab content
        tabContents.forEach(content => {
            content.classList.toggle('active', content.id === tabId);
        });
        
        // Initialize charts when visualization tab is selected
        if (tabId === 'visualization') {
            fetchVisualizationData();
        }
    }
    
    // Calculate BMI
    function calculateBMI() {
        const weight = parseFloat(weightInput.value);
        const height = parseFloat(heightInput.value);

        if (!(weight > 0 && height > 0)) {
            showError('Please enter valid weight and height.');
            return;
        }
        
        if (weight > 0 && height > 0) {
            fetch('/calculate_bmi', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ weight, height })
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    showError(data.error);
                } else {
                    bmiInput.value = data.bmi;
                    
                    // Add BMI category text
                    let bmiCategory = '';
                    if (data.bmi < 18.5) {
                        bmiCategory = ' (Underweight)';
                    } else if (data.bmi < 25) {
                        bmiCategory = ' (Normal)';
                    } else if (data.bmi < 30) {
                        bmiCategory = ' (Overweight)';
                    } else {
                        bmiCategory = ' (Obese)';
                    }
                    
                    bmiInput.value = `${data.bmi}${bmiCategory}`;
                }
            })
            .catch(error => {
                showError('Failed to calculate BMI: ' + error);
            });
        }
    }
    
    // Generate diet recommendations
    function generateRecommendations() {
        // Validate required fields
        if (!validateForm()) {
            return;
        }
        
        // Show loading overlay
        loadingOverlay.style.display = 'flex';
        
        // Collect form data
        const formData = {
            age: parseInt(ageInput.value),
            gender: genderSelect.value,
            weight: parseFloat(weightInput.value),
            height: parseFloat(heightInput.value),
            activity_level: activityLevelSelect.value,
            goal: goalSelect.value,
            vegetarian: vegetarianCheckbox.checked,
            vegan: veganCheckbox.checked,
            low_carb: lowCarbCheckbox.checked,
            low_fat: lowFatCheckbox.checked,
            high_protein: highProteinCheckbox.checked,
            allergies: allergiesInput.value
        };
        
        // Send request to API
        fetch('/generate_recommendations', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(formData)
        })
        .then(response => response.json())
        .then(data => {
            loadingOverlay.style.display = 'none';
            
            if (data.error) {
                showError(data.error);
            } else {
                displayRecommendations(data);
                switchTab('recommendations');
            }
        })
        .catch(error => {
            loadingOverlay.style.display = 'none';
            showError('Failed to generate recommendations: ' + error);
        });
    }
    
    // Validate form inputs
    function validateForm() {
        if (!ageInput.value || ageInput.value <= 0) {
            showError('Please enter a valid age.');
            return false;
        }
        
        if (!weightInput.value || weightInput.value <= 0) {
            showError('Please enter a valid weight.');
            return false;
        }
        
        if (!heightInput.value || heightInput.value <= 0) {
            showError('Please enter a valid height.');
            return false;
        }
        
        return true;
    }
    
    // Display recommendations
    function displayRecommendations(data) {
        // Display nutrition requirements
        const nutritionReq = data.nutrition_req;
        document.getElementById('nutrition-requirements').innerHTML = `
            <p><strong>Daily Calories:</strong> ${nutritionReq.calories} kcal</p>
            <p><strong>Protein:</strong> ${nutritionReq.protein}g</p>
            <p><strong>Carbohydrates:</strong> ${nutritionReq.carbs}g</p>
            <p><strong>Fat:</strong> ${nutritionReq.fat}g</p>
            <p><strong>Fiber:</strong> ${nutritionReq.fiber}g</p>
        `;
        
        // Display meal plan
        const mealPlan = data.meal_plan;
        
        // Clear previous meal items
        document.getElementById('breakfast').innerHTML = '';
        document.getElementById('lunch').innerHTML = '';
        document.getElementById('dinner').innerHTML = '';
        document.getElementById('snacks').innerHTML = '';
        
        // Add meal items for each meal type
        displayMealItems('breakfast', mealPlan.breakfast);
        displayMealItems('lunch', mealPlan.lunch);
        displayMealItems('dinner', mealPlan.dinner);
        displayMealItems('snacks', mealPlan.snacks);
    }
    
    // Display meal items for a specific meal type
    function displayMealItems(mealId, items) {
        const container = document.getElementById(mealId);

        if (items.length === 0) {
            container.textContent = 'No food items available based on your preferences.';
            return;
        }
        
        items.forEach(item => {
            const mealItem = document.createElement('div');
            mealItem.className = 'meal-item';
            
            mealItem.innerHTML = `
                <h5>${item.Food_items}</h5>
                <p><strong>Category:</strong> ${item.Category}</p>
                <p><strong>Calories:</strong> ${item.Calories} kcal</p>
                <p><strong>Protein:</strong> ${item.Protein}g</p>
                <p><strong>Carbs:</strong> ${item.Carbohydrates}g</p>
                <p><strong>Fat:</strong> ${item.Fats}g</p>
                <p><strong>Fiber:</strong> ${item.Fibre}g</p>
            `;
            
            container.appendChild(mealItem);
        });
    }
    
    // Fetch data for visualizations
    function fetchVisualizationData() {
        fetch('/get_visualizations_data')
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    showError(data.error);
                } else {
                    createVisualizationCharts(data);
                }
            })
            .catch(error => {
                showError('Failed to load visualization data: ' + error);
            });
    }
    
    // Create visualization charts
    function createVisualizationCharts(data) {
        // Destroy existing charts to prevent duplicates
        if (macrosChart) macrosChart.destroy();
        if (mealCaloriesChart) mealCaloriesChart.destroy();
        if (nutrientComparisonChart) nutrientComparisonChart.destroy();
        
        // Create macronutrient breakdown chart (pie chart)
        const macrosCtx = document.getElementById('macros-chart').getContext('2d');
        macrosChart = new Chart(macrosCtx, {
            type: 'pie',
            data: {
                labels: data.macros.labels,
                datasets: [{
                    data: data.macros.values,
                    backgroundColor: [
                        '#4CAF50',  // Protein - green
                        '#FF9800',  // Carbs - orange
                        '#2196F3'   // Fat - blue
                    ],
                    borderWidth: 1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'bottom'
                    },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                const percentage = Math.round((context.raw / total) * 100);
                                return `${context.label}: ${percentage}%`;
                            }
                        }
                    }
                }
            }
        });
        
        // Create meal calories chart (bar chart)
        const mealCaloriesCtx = document.getElementById('meal-calories-chart').getContext('2d');
        mealCaloriesChart = new Chart(mealCaloriesCtx, {
            type: 'bar',
            data: {
                labels: data.meal_calories.labels.map(label => label.charAt(0).toUpperCase() + label.slice(1)),
                datasets: [{
                    label: 'Calories',
                    data: data.meal_calories.values,
                    backgroundColor: '#81C784',
                    borderColor: '#4CAF50',
                    borderWidth: 1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        title: {
                            display: true,
                            text: 'Calories (kcal)'
                        }
                    }
                }
            }
        });
        
        // Create nutrient comparison chart (bar chart)
        const nutrientComparisonCtx = document.getElementById('nutrient-comparison-chart').getContext('2d');
        nutrientComparisonChart = new Chart(nutrientComparisonCtx, {
            type: 'bar',
            data: {
                labels: data.nutrient_comparison.nutrients,
                datasets: [
                    {
                        label: 'Recommended',
                        data: data.nutrient_comparison.recommended,
                        backgroundColor: '#81C784',
                        borderColor: '#4CAF50',
                        borderWidth: 1
                    },
                    {
                        label: 'Actual in Meal Plan',
                        data: data.nutrient_comparison.actual,
                        backgroundColor: '#FF9800',
                        borderColor: '#F57C00',
                        borderWidth: 1
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        title: {
                            display: true,
                            text: 'Amount (g)'
                        }
                    }
                }
            }
        });
    }
    
    // Show error modal
    function showError(message) {
        errorMessage.textContent = message;
        errorModal.style.display = 'block';
    }
});
